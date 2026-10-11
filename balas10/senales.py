"""Reglas de 30 balas 10x con dos cargadores (configuración D del estudio 2026-10-09, motor de backtest b10.py).

Todo se decide al cierre de una vela de 4 h (UTC). Las series diarias (régimen semanal, volatilidad, Mayer y momento)
se actualizan con el cierre diario de las 00:00 UTC y valen hasta el próximo cierre diario.

  Régimen:   cierre del domingo > EMA20 de los cierres semanales (domingos) y esa EMA > la de 4 semanas antes.
             Se fija con el cierre del domingo y vale toda la semana siguiente.
  Entrada:   régimen activo, RSI(2) 4h ≤ 15 y retorno de 90 días > 0.
  Salida:    el cierre 4h estuvo sobre la SMA80 en algún momento de la campaña y ahora cruza hacia abajo
             (cierre anterior sobre su SMA, cierre actual debajo), o al cierre diario el régimen está apagado.
  Recarga:   al cierre diario, según el ROE de la campaña: ≥0 → 1 bala; ≥−5% → 2; ≥−10% → 4; ≥−15% → 6; peor → 8.
             Proporción a posición: ROE ≥ −10% → 100%; ≥ −20% → 70%; ≥ −30% → 40%; peor → 0% (el resto, sólo margen).
             Cuando el cargador 1 se agota, las balas salen del cargador 2 y van enteras a margen.
  Apalancamiento de cada bala = 10 × min(1, max(0,33, 0,60 / volatilidad anual de 60 días)) × (0,5 si precio/SMA200 > 2,2).
"""
import math

import numpy as np
import pandas as pd

P = dict(
    lev=10.0,          # apalancamiento base
    B1=30, B2=30,      # balas de cada cargador
    f1=0.40,           # cargador 1 (fracción del capital de la campaña)
    fr=0.40,           # reserva de margen
    f2=0.20,           # cargador 2 (sólo margen)
    pref=0.50,         # parte de la reserva que se pone al abrir
    rt=0.12,           # el resto de la reserva entra si la distancia a la liquidación es ≤ 12 %
    init=1,            # balas iniciales
    rsi_max=15.0, ma=80, ema_sem=20, pendiente=4,
    vt_dias=60, vt_obj=0.60, vt_piso=0.33, mayer=2.2, mayer_mult=0.5, mom=90,
)


def rsi2(c):
    """RSI(2) de Wilder (igual que el backtest: medias exponenciales con alfa 1/2)."""
    d = c.diff()
    au = d.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    ad = (-d.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    return 100 - 100 / (1 + au / ad)


def diario(dC, p=P):
    """dC: cierres diarios (índice = día de apertura, UTC), el último es el último día CERRADO.
    Devuelve el régimen semanal vigente, el multiplicador de apalancamiento, el momento y los valores intermedios."""
    dC = dC.dropna().astype(float)
    dom = dC[dC.index.dayofweek == 6]                     # cierres de domingo (la semana cierra el domingo 24:00 UTC)
    a = 2.0 / (p["ema_sem"] + 1)
    ema = []
    e = None
    for x in dom.values:
        e = x if e is None else e + a * (x - e)
        ema.append(e)
    reg = False
    if len(ema) > p["pendiente"]:
        reg = bool(dom.values[-1] > ema[-1] and ema[-1] > ema[-1 - p["pendiente"]])
    v = dC.values
    nd = len(v)
    vol = None
    lm = 1.0
    if nd > p["vt_dias"]:
        r = np.diff(np.log(v[-(p["vt_dias"] + 1):]))
        vol = float(r.std(ddof=1) * math.sqrt(365))
        if vol > 0:
            lm = max(p["vt_piso"], min(1.0, p["vt_obj"] / vol))
    mayer = None
    if nd >= 200:
        mayer = float(v[-1] / v[-200:].mean())
        if mayer > p["mayer"]:
            lm *= p["mayer_mult"]
    mom = bool(v[-1] / v[-1 - p["mom"]] - 1 > 0) if nd > p["mom"] else False
    ret90 = float(v[-1] / v[-1 - p["mom"]] - 1) if nd > p["mom"] else None
    return dict(regimen=reg, levm=float(lm), momento=mom, vol=vol, mayer=mayer, ret90=ret90,
                ema_sem=float(ema[-1]) if ema else None, cierre_dom=float(dom.values[-1]) if len(dom) else None,
                ultimo_dia=str(dC.index[-1].date()) if nd else None)


def cuatro_h(c4, p=P):
    """c4: cierres de 4 h (índice = apertura de la vela, UTC), el último es la vela recién CERRADA."""
    c4 = c4.dropna().astype(float)
    ma = c4.rolling(p["ma"]).mean()
    r = rsi2(c4)
    c, m = float(c4.iloc[-1]), float(ma.iloc[-1])
    return dict(cierre=c, sma=m, sobre=bool(c > m), sobre_previa=bool(c4.iloc[-2] > ma.iloc[-2]),
                rsi=float(r.iloc[-1]), vela=str(c4.index[-1]))


def balas_por_roe(roe):
    add = 1 if roe >= 0 else 2 if roe >= -0.05 else 4 if roe >= -0.10 else 6 if roe >= -0.15 else 8
    ratio = 1.0 if roe >= -0.10 else 0.7 if roe >= -0.20 else 0.4 if roe >= -0.30 else 0.0
    return add, ratio


def entrada(d, h, p=P):
    return bool(d["regimen"] and h["rsi"] <= p["rsi_max"] and d["momento"])


def senal(c4, dC, t_cierre, p=P):
    """Señales al cierre `t_cierre` (= apertura de la vela siguiente). `dia` = este cierre es también el diario."""
    t_cierre = pd.Timestamp(t_cierre)
    h = cuatro_h(c4, p)
    d = diario(dC, p)
    return dict(t=str(t_cierre), dia=bool(t_cierre.hour == 0), **h, **d, entrada=entrada(d, h, p))


def liquidacion_inversa(contratos, margen_btc, entrada_px, mmr=0.007):
    """Precio de liquidación aproximado de un largo inverso (1 contrato = 1 USD) con margen aislado."""
    if contratos <= 0:
        return None
    return contratos * (1 + mmr) / (margen_btc + contratos / entrada_px)
