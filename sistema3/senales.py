"""Señales del Sistema 3 (FR20 + modo BTC + cortos Aberration 0,5×), tal cual la especificación congelada
`Estrategia_FR20_BTC_cortos_especificacion.md`. Funciones puras sobre DataFrames: no tocan el exchange ni la base.

Convenciones:
  · velas diarias indexadas por el día UTC en que ABREN; la última fila es el último día cerrado (t);
  · velas de 4 h indexadas por la hora en que abren; la última fila es la última vela cerrada;
  · DataFrames anchos: filas = tiempo, columnas = símbolo (ticker base, p. ej. "SOL").
"""
import math

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------------------------- parámetros congelados
SMA_BTC, ROC_BTC = 140, 84                 # filtro de BTC
SMA_AMP, AMP_ON, AMP_OFF = 50, 0.70, 0.50  # amplitud con histéresis
DIAS_ARRANQUE_AMP = 200                    # el estado de amplitud se reconstruye desde 200 días atrás, empezando apagado
L_FR, ROC_ENTRADA_FR, ROC_ORDEN_FR = 20, 10, 20
CUPOS_ALTS, PESO_MAX_ALT = 10, 0.10
MIN_DIAS_HIST, MIN_VOL_USD, DIAS_VOL = 90, 2_000_000.0, 30

AB_N, AB_K, AB_ROC = 120, 2.0, 540         # Aberration 4h: SMA120, 2 desviaciones (poblacional), ROC540 (90 días)
AB_DERRUMBE = -0.20                        # no entrar si la vela de señal cayó más de 20 %
CUPOS_CORTOS, NOCIONAL_CORTO = 5, 0.10
AB_DIAS_SIN_PRECIO = 6
MIN_DIAS_PERP = 7

# Niveles de riesgo (mismas reglas; sólo cambian dos perillas). Ver Sistema3_Ranking_y_Niveles_de_Riesgo_2026-10-07.
NIVELES = {
    "conservador": dict(riesgo_alt=0.0040, btc=0.75),
    "moderado": dict(riesgo_alt=0.0050, btc=1.00),
    "agresivo": dict(riesgo_alt=0.006667, btc=1.00),   # el de la especificación original
}


# ---------------------------------------------------------------------------------------------- indicadores
def sma(x, n):
    return x.rolling(n, min_periods=n).mean()


def roc(x, n):
    return x / x.shift(n) - 1


def atr_wilder(h, l, c, n=14):
    """ATR de Wilder (α = 1/n), arrancando desde el primer TR disponible. Acepta Series o DataFrames alineados."""
    pc = c.shift(1)
    a = (h - l)
    b = (h - pc).abs()
    d = (l - pc).abs()
    tr = np.maximum(np.maximum(a, b.fillna(a)), d.fillna(a))
    return tr.ewm(alpha=1 / n, adjust=False, ignore_na=True).mean()


# ---------------------------------------------------------------------------------------------- parte A (diaria)
def filtro_btc(c_btc):
    """Serie booleana: cierre de BTC > SMA140 y ROC84 > 0."""
    return (c_btc > sma(c_btc, SMA_BTC)) & (roc(c_btc, ROC_BTC) > 0)


def elegibles(C, V, top50, excluir=()):
    """Matriz booleana de elegibles por día (A3 puntos 2-4) para una lista de top 50 dada.
    C: cierres diarios; V: volumen diario en USDT (quote volume). top50: iterable de símbolos (o dict día→set).
    Historia ≥ 90 velas y mediana del volumen de los últimos 30 días ≥ 2 M USD (ambas con datos hasta el día)."""
    hist = C.notna().cumsum()
    medv = V.rolling(DIAS_VOL, min_periods=1).median()
    E = (hist >= MIN_DIAS_HIST) & (medv >= MIN_VOL_USD) & C.notna()
    cols = set(top50) - set(excluir)
    for c in E.columns:
        if c not in cols:
            E[c] = False
    return E


def amplitud(C, E):
    """% de elegibles con cierre > SMA50, por día."""
    arriba = (C > sma(C, SMA_AMP)) & E
    n = E.sum(axis=1)
    return (arriba.sum(axis=1) / n.replace(0, np.nan)).fillna(0.0)


def estado_amplitud(amp, previo=None, desde=None):
    """Recorre la amplitud día por día con histéresis 70/50. previo: estado guardado (bool) al día anterior a `desde`.
    Sin estado guardado: arranca apagado 200 días antes del final. Devuelve Series booleana."""
    if previo is None or desde is None:
        amp = amp.iloc[-DIAS_ARRANQUE_AMP:]
        on = False
    else:
        amp = amp[amp.index >= desde]
        on = bool(previo)
    out = {}
    for d, a in amp.items():
        if not on and a > AMP_ON:
            on = True
        elif on and a < AMP_OFF:
            on = False
        out[d] = on
    return pd.Series(out, dtype=bool)


def modo_del_dia(btc_ok, amp_on):
    if not btc_ok:
        return "USDT"
    return "ALTS" if amp_on else "BTC"


def fr20(C, H, L, c_btc, E_hoy, cartera, cupos=None, riesgo_alt=NIVELES["moderado"]["riesgo_alt"]):
    """Rutina FR20 del día t (última fila). Devuelve dict(ventas, candidatas, detalle).
    ventas: alts en cartera cuyo par alt/BTC cerró bajo su SMA20 (las que no tienen vela hoy se mantienen).
    candidatas: lista ordenada [dict(sim, roc20, atrp, peso)] de las primeras N = cupos libres (después de las ventas)."""
    t = C.index[-1]
    R = C.div(c_btc, axis=0)
    smaR = sma(R, L_FR)
    hoy = dict(R=R.loc[t], smaR=smaR.loc[t], roc10=roc(R, ROC_ENTRADA_FR).loc[t], roc20=roc(R, ROC_ORDEN_FR).loc[t],
               c=C.loc[t], smac=sma(C, L_FR).loc[t])
    ventas = []
    for s in cartera:
        if s not in C.columns or pd.isna(hoy["R"].get(s)) or pd.isna(hoy["smaR"].get(s)):
            continue                                      # sin vela hoy: se mantiene
        if hoy["R"][s] < hoy["smaR"][s]:
            ventas.append(s)
    quedan = [s for s in cartera if s not in ventas]
    libres = (CUPOS_ALTS - len(quedan)) if cupos is None else cupos
    atrp = (atr_wilder(H, L, C) / C).loc[t]
    cand = []
    for s in C.columns:
        if s == "BTC" or s in cartera or not bool(E_hoy.get(s, False)):
            continue
        r, m, r10, r20, c, mc = (hoy[k].get(s) for k in ("R", "smaR", "roc10", "roc20", "c", "smac"))
        if any(pd.isna(x) for x in (r, m, r10, r20, c, mc)):
            continue
        if r > m and r10 > 0 and c > mc:
            a = float(atrp.get(s)) if pd.notna(atrp.get(s)) else math.nan
            if not a or a != a or a <= 0:
                continue
            cand.append(dict(sim=s, roc20=float(r20), atrp=a, peso=min(PESO_MAX_ALT, riesgo_alt / a)))
    cand.sort(key=lambda x: -x["roc20"])
    return dict(ventas=ventas, candidatas=cand[:max(libres, 0)], ranking=cand[:15],
                atrp={k: float(v) for k, v in atrp.items() if pd.notna(v)})


# ---------------------------------------------------------------------------------------------- parte B (4 h)
def regimen_cortos(c4_btc):
    """True si ROC540 de BTC (4 h) < 0 en la última vela cerrada."""
    r = roc(c4_btc, AB_ROC).iloc[-1]
    return bool(pd.notna(r) and r < 0), (float(r) if pd.notna(r) else None)


def aberration(O4, C4, abiertos, universo):
    """Rutina de cortos en la última vela de 4 h cerrada de C4.
    abiertos: símbolos con corto abierto. universo: símbolos que pueden abrirse (B2).
    Devuelve dict(salidas, sin_precio, candidatas ordenadas [dict(sim, roc540, cierre, banda)])."""
    t = C4.index[-1]
    m = sma(C4, AB_N)
    sd = C4.rolling(AB_N, min_periods=AB_N).std(ddof=0)
    r = roc(C4, AB_ROC)
    salidas, sin_precio = [], []
    for s in abiertos:
        if s not in C4.columns:
            sin_precio.append(s)
            continue
        col = C4[s]
        ult = col.last_valid_index()
        if ult is None or (t - ult) > pd.Timedelta(days=AB_DIAS_SIN_PRECIO):
            sin_precio.append(s)
            continue
        if pd.notna(col.loc[t]) and pd.notna(m[s].loc[t]) and col.loc[t] > m[s].loc[t]:
            salidas.append(s)
    cand = []
    for s in universo:
        if s == "BTC" or s in abiertos or s not in C4.columns:
            continue
        c, mm, d, rr, o = C4[s].loc[t], m[s].loc[t], sd[s].loc[t], r[s].loc[t], O4[s].loc[t]
        if any(pd.isna(x) for x in (c, mm, d, rr, o)) or o <= 0:
            continue
        banda = mm - AB_K * d
        if c < banda and rr < 0 and (c / o - 1) > AB_DERRUMBE:
            cand.append(dict(sim=s, roc540=float(rr), cierre=float(c), banda=float(banda)))
    cand.sort(key=lambda x: x["roc540"])
    return dict(salidas=salidas, sin_precio=sin_precio, candidatas=cand)
