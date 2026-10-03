"""Estadísticas para la web y los resúmenes de Telegram (todo se calcula desde la base)."""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .config import RAIZ

BT_DIR = RAIZ / "datos_backtest"
INICIO_PRUEBA = "2024-01-01"     # la banda esperada usa sólo el período de prueba de la validación (no el de ajuste)


def _ms(t):
    return int(pd.Timestamp(t).value // 10**6)


def serie_patrimonio(db, cuenta="total", desde_ms=0):
    f = db.filas("SELECT ts, patrimonio FROM patrimonio WHERE cuenta=? AND ts>=? ORDER BY ts", (cuenta, desde_ms))
    if not f:
        return pd.Series(dtype=float)
    s = pd.Series([r["patrimonio"] for r in f], index=pd.to_datetime([r["ts"] for r in f], unit="ms"))
    return s[s > 0]


def flujos(db):
    """Aportes (+) y retiros (−) registrados con `cli aporte`, para no confundirlos con rendimiento."""
    f = db.filas("SELECT ts, monto FROM flujos ORDER BY ts")
    if not f:
        return pd.Series(dtype=float)
    return pd.Series([r["monto"] for r in f], index=pd.to_datetime([r["ts"] for r in f], unit="ms"))


def valor_cuota(db, cuenta="total"):
    """Serie de valor cuota (arranca en 1): neutraliza aportes y retiros."""
    s = serie_patrimonio(db, cuenta)
    if s.empty:
        return s
    fl = flujos(db)
    if fl.empty:
        return s / s.iloc[0]
    v = [1.0]
    for i in range(1, len(s)):
        f = fl[(fl.index > s.index[i - 1]) & (fl.index <= s.index[i])].sum()
        v.append(v[-1] * (s.iloc[i] - f) / s.iloc[i - 1])
    return pd.Series(v, index=s.index)


def rendimientos(cuota):
    if cuota.empty:
        return dict(diario=[], semanal=[], mensual=[])
    out = {}
    for k, regla, fmt in (("diario", "D", "%Y-%m-%d"), ("semanal", "W-SUN", "%Y-%m-%d"), ("mensual", "ME", "%Y-%m")):
        c = cuota.resample(regla).last().dropna()
        base = pd.concat([pd.Series([cuota.iloc[0]], index=[c.index[0] - pd.Timedelta(seconds=1)]), c])
        r = base.pct_change().dropna()
        out[k] = [dict(periodo=i.strftime(fmt), rend=float(x)) for i, x in r.items()][::-1]
    return out


def kpis(db, cfg):
    cuota = valor_cuota(db, "total")
    s = serie_patrimonio(db, "total")
    u = db.get("ultimo_ciclo") or {}
    d = dict(modo=cfg.modo, nivel=cfg.nivel, p95_is=cfg.p95_is, alerta=cfg.alerta_caida, corte=cfg.corte_caida,
             pausado=bool(db.get("pausado")), bloqueado=bool(db.get("bloqueado")), conciliacion_ok=bool(db.get("conciliacion_ok", True)),
             ultimo_ciclo=u.get("t"), latido=db.get("latido"), version=db.get("version"))
    if s.empty:
        return d
    pk = cuota.cummax()
    dias = max((s.index[-1] - s.index[0]).total_seconds() / 86400, 1e-9)
    rd = cuota.resample("D").last().dropna().pct_change().dropna()
    d.update(patrimonio=float(s.iloc[-1]), inicio=s.index[0].isoformat(), patrimonio_inicial=float(s.iloc[0]),
             rend_total=float(cuota.iloc[-1] - 1), dias=dias,
             cagr=float(cuota.iloc[-1] ** (365 / dias) - 1) if dias >= 60 else None,
             caida_actual=float(cuota.iloc[-1] / pk.iloc[-1] - 1), caida_max=float((cuota / pk - 1).min()),
             sharpe=float(rd.mean() / rd.std() * math.sqrt(365)) if len(rd) > 20 and rd.std() > 0 else None,
             principal=float(u.get("E_main") or 0), balas=float(u.get("E_bal") or 0))
    for k, dt in (("rend_hoy", "D"), ("rend_semana", "W-SUN"), ("rend_mes", "ME")):
        per = cuota.index[-1].to_period({"D": "D", "W-SUN": "W-SUN", "ME": "M"}[dt]).start_time
        prev = cuota[cuota.index < per]
        d[k] = float(cuota.iloc[-1] / (prev.iloc[-1] if len(prev) else cuota.iloc[0]) - 1)
    return d


def posiciones(db):
    precios = db.get("precios") or {}
    out = []
    for L in db.filas("SELECT * FROM lotes WHERE cerrado_ts IS NULL ORDER BY abierto_ts"):
        px = precios.get(L["simbolo"])
        n = abs(L["contratos"]) * L["tam_contrato"]
        out.append(dict(id=L["id"], estrategia=L["estrategia"], simbolo=L["simbolo"], lado="largo" if L["lado"] > 0 else "corto",
                        contratos=L["contratos"], entrada=L["precio_entrada"], precio=px, stop=L["stop"],
                        nocional=n * (px or L["precio_entrada"]), pnl=L["lado"] * n * (px - L["precio_entrada"]) if px else None,
                        pnl_pct=L["lado"] * (px / L["precio_entrada"] - 1) if px else None,
                        abierto=pd.to_datetime(L["abierto_ts"], unit="ms").isoformat()))
    b = db.get("balas") or {}
    if b.get("activo"):
        out.append(dict(id="balas", estrategia="balas5", simbolo="XBTUSDM", lado="largo", contratos=round(b["ntn"] * b["W"]),
                        entrada=None, precio=b.get("ultimo_precio"), stop=None, nocional=b["ntn"] * b["W"],
                        pnl=b.get("eq", 0) - b["W"], pnl_pct=b.get("eq", 0) / b["W"] - 1, abierto=b.get("entry_t"),
                        extra=dict(balas_usadas=b.get("used"), dist_liquidacion=b.get("dist_liq"), reserva=b.get("resd"))))
    return out


def por_estrategia(db):
    out = {}
    for r in db.filas("SELECT estrategia, COUNT(*) n, SUM(pnl>0) g, SUM(pnl) pnl, AVG(pnl) media,"
                      " SUM(CASE WHEN cerrado_ts IS NULL THEN 1 ELSE 0 END) abiertos FROM lotes GROUP BY estrategia"):
        out[r["estrategia"]] = dict(lotes=r["n"], abiertos=r["abiertos"], ganadoras=(r["g"] or 0) / max(r["n"] - r["abiertos"], 1),
                                    pnl=r["pnl"] or 0.0, pnl_medio=r["media"] or 0.0)
    b = db.get("balas") or {}
    if b:
        ent = db.filas("SELECT COUNT(*) n FROM operaciones WHERE estrategia='balas5' AND motivo='entrada'")[0]["n"]
        out["balas5"] = dict(lotes=ent, abiertos=int(bool(b.get("activo"))), ganadoras=None, pnl=(b.get("eq") or 0) - (db.get("balas_inicial") or b.get("W", 0)),
                             pnl_medio=None, liquidaciones=b.get("liqs", 0))
    asig = db.filas("SELECT * FROM asignacion WHERE ts=(SELECT MAX(ts) FROM asignacion)")
    E = (db.get("ultimo_ciclo") or {}).get("E") or 0
    for a in asig:   # en fracción del patrimonio total
        out.setdefault(a["estrategia"], {}).update(pedido=a["pedido"] / E if E else None, concedido=a["concedido"] / E if E else None,
                                                   pedido_usdt=a["pedido"], concedido_usdt=a["concedido"], escala=a["nocional"])
    if b.get("activo") and E:
        out["balas5"].update(pedido=b["ntn"] * b["W"] / E, concedido=b["ntn"] * b["W"] / E)
    return out


# ------------------------------------------------------------ backtest
def backtest():
    if not (BT_DIR / "cascada_niveles.csv").exists():
        return {}
    niv = pd.read_csv(BT_DIR / "cascada_niveles.csv", index_col=0, parse_dates=True)
    est = pd.read_csv(BT_DIR / "estrategias.csv", index_col=0, parse_dates=True)
    res = json.loads((BT_DIR / "resumen.json").read_text())
    sem = lambda d: d.resample("W-SUN").last()
    return dict(fechas=[x.strftime("%Y-%m-%d") for x in sem(niv).index],
                niveles={k: sem(niv)[k].round(4).tolist() for k in niv},
                estrategias={k: sem(est)[k].round(4).tolist() for k in est}, resumen=res)


def banda_esperada(cfg, dias):
    """Rango de rendimiento y de caída máxima del backtest del nivel para ventanas de `dias` días (percentiles 5/50/95)."""
    f = BT_DIR / "cascada_niveles.csv"
    if not f.exists() or dias < 1:
        return None
    eq = pd.read_csv(f, index_col=0, parse_dates=True)[cfg.nivel].loc[INICIO_PRUEBA:]
    n = int(round(dias))
    if n >= len(eq) - 30:
        return None
    r = (eq.shift(-n) / eq - 1).dropna()
    cdd = []
    v = eq.values
    for i in range(0, len(v) - n, 3):
        w = v[i:i + n + 1]; cdd.append((w / np.maximum.accumulate(w) - 1).min())
    p = lambda x, q: float(np.percentile(x, q))
    return dict(dias=n, rend=dict(p5=p(r, 5), p50=p(r, 50), p95=p(r, 95)), caida=dict(p5=p(cdd, 5), p50=p(cdd, 50), p95=p(cdd, 95)))


def comparacion_vivo(db, cfg):
    """Curva en vivo contra la del backtest del nivel y su banda 5–95 % desde el mismo día de inicio."""
    cuota = valor_cuota(db, "total")
    f = BT_DIR / "cascada_niveles.csv"
    if cuota.empty or not f.exists():
        return {}
    eq = pd.read_csv(f, index_col=0, parse_dates=True)[cfg.nivel].loc[INICIO_PRUEBA:]
    viv = cuota.resample("D").last().dropna()
    n = len(viv)
    r = eq.pct_change().dropna().values
    # trayectorias del backtest de igual largo, desde cada día posible
    m = len(r) - n
    if n < 2 or m < 30:
        return dict(fechas=[x.strftime("%Y-%m-%d") for x in viv.index], vivo=viv.round(5).tolist())
    idx = np.arange(0, m, 2)
    tray = np.cumprod(1 + np.stack([r[i:i + n - 1] for i in idx]), axis=1)
    tray = np.hstack([np.ones((len(idx), 1)), tray])
    return dict(fechas=[x.strftime("%Y-%m-%d") for x in viv.index], vivo=viv.round(5).tolist(),
                p5=np.percentile(tray, 5, axis=0).round(5).tolist(), p50=np.percentile(tray, 50, axis=0).round(5).tolist(),
                p95=np.percentile(tray, 95, axis=0).round(5).tolist())


def texto_estado(db, cfg):
    k = kpis(db, cfg)
    if "patrimonio" not in k:
        return "Todavía no hay datos de patrimonio."
    pos = posiciones(db)
    f = lambda x: "—" if x is None else f"{x:+.2%}"
    lin = [f"Modo {k['modo']} · {k['nivel']}" + (" · PAUSADO" if k["pausado"] else "") + (" · BLOQUEADO" if k["bloqueado"] else ""),
           f"Patrimonio {k['patrimonio']:,.2f} USDT (principal {k['principal']:,.0f} + balas {k['balas']:,.0f})",
           f"Hoy {f(k['rend_hoy'])} · semana {f(k['rend_semana'])} · mes {f(k['rend_mes'])} · total {f(k['rend_total'])}",
           f"Caída actual {k['caida_actual']:.1%} · máxima {k['caida_max']:.1%} (alerta {k['alerta']:.0%}, corte {k['corte']:.0%})",
           f"Posiciones: {len(pos)}"]
    for p in pos[:15]:
        lin.append(f"  {p['estrategia']} {p['lado']} {p['simbolo']} {p['nocional']:,.0f} USDT {f(p['pnl_pct'])}")
    lin.append(f"Último ciclo: {k['ultimo_ciclo']}")
    return "\n".join(lin)
