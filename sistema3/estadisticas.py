"""Estadísticas para la web y Telegram: valor cuota (descuenta aportes y retiros), rendimientos, caída, posiciones."""
import time

import numpy as np
import pandas as pd


def serie(db, desde_ms=0):
    f = db.filas("SELECT ts, total, spot, futuros, btc, expo_largos, expo_cortos FROM patrimonio WHERE ts>=? ORDER BY ts", (desde_ms,))
    if not f:
        return pd.DataFrame(columns=["total", "spot", "futuros", "btc", "expo_largos", "expo_cortos"])
    d = pd.DataFrame(f)
    d.index = pd.to_datetime(d.pop("ts"), unit="ms")
    return d


def cuota(db):
    """Valor cuota: arranca en 1 y sólo se mueve por resultados (los aportes o retiros de `flujos` no lo cambian)."""
    d = serie(db)
    if d.empty:
        return pd.Series(dtype=float)
    fl = db.filas("SELECT ts, monto FROM flujos ORDER BY ts")
    tot = d["total"]
    ret = tot.pct_change().fillna(0.0)
    for x in fl:                                       # el aporte se resta del rendimiento del tramo en que entró
        t = pd.to_datetime(x["ts"], unit="ms")
        i = tot.index.searchsorted(t)
        if 0 < i < len(tot):
            ret.iloc[i] = (tot.iloc[i] - x["monto"]) / tot.iloc[i - 1] - 1
    return (1 + ret).cumprod()


def rendimientos(q):
    if q.empty:
        return dict(diario=[], semanal=[], mensual=[])
    out = {}
    for nombre, regla, fmt, n in (("diario", "D", "%Y-%m-%d", 60), ("semanal", "W-SUN", "%Y-%m-%d", 52), ("mensual", "ME", "%Y-%m", 36)):
        u = q.resample(regla).last().dropna()
        base = pd.concat([pd.Series([1.0], index=[u.index[0] - pd.Timedelta(days=1)]), u]) if len(u) else u
        r = (base / base.shift() - 1).dropna()
        out[nombre] = [dict(periodo=i.strftime(fmt), r=float(v)) for i, v in r.iloc[-n:].items()][::-1]
    return out


def kpis(db):
    d = serie(db)
    q = cuota(db)
    if d.empty or q.empty:
        return dict(hay=False)
    ahora = q.index[-1]

    def desde(dias):
        prev = q[q.index <= ahora - pd.Timedelta(days=dias)]
        return float(q.iloc[-1] / prev.iloc[-1] - 1) if len(prev) else float(q.iloc[-1] - 1)

    hoy = q[q.index >= ahora.normalize()]
    base_hoy = q[q.index < ahora.normalize()]
    dd = q / q.cummax() - 1
    dia = q.resample("D").last().dropna()
    rd = dia.pct_change().dropna()
    dias = (q.index[-1] - q.index[0]).total_seconds() / 86400
    cagr = float(q.iloc[-1] ** (365 / dias) - 1) if dias > 30 else None
    aportes = sum(x["monto"] for x in db.filas("SELECT monto FROM flujos"))
    return dict(hay=True, total=float(d["total"].iloc[-1]), spot=float(d["spot"].iloc[-1]), futuros=float(d["futuros"].iloc[-1]),
                hoy=float(q.iloc[-1] / base_hoy.iloc[-1] - 1) if len(base_hoy) else float(q.iloc[-1] / hoy.iloc[0] - 1),
                semana=desde(7), mes=desde(30), total_pct=float(q.iloc[-1] - 1), cagr=cagr,
                caida=float(dd.iloc[-1]), caida_max=float(dd.min()),
                sharpe=float(rd.mean() / rd.std() * np.sqrt(365)) if len(rd) > 20 and rd.std() > 0 else None,
                expo_largos=float(d["expo_largos"].iloc[-1]), expo_cortos=float(d["expo_cortos"].iloc[-1]),
                desde=q.index[0].strftime("%Y-%m-%d"), dias=round(dias, 1), aportes=aportes,
                actualizado=d.index[-1].strftime("%Y-%m-%d %H:%M"))


def posiciones(motor, valuacion=None):
    L = motor.libro
    v = valuacion or motor.valuar(L)
    T = v["T"] or 1
    out = []
    for s, p in list(L["alts"].items()) + ([("BTC", L["btc"])] if L["btc"] else []):
        px = v["precios"].get(s)
        valor = p["cant"] * px if px else None
        out.append(dict(parte="Largo", simbolo=s, cantidad=p["cant"], entrada=p["costo"] / p["cant"] if p["cant"] else None,
                        precio=px, valor=valor, peso=valor / T if valor else None,
                        resultado=(valor / p["costo"] - 1) if valor and p["costo"] else None, mitad=bool(p.get("reducida")),
                        desde=pd.to_datetime(p["ts"], unit="ms").strftime("%Y-%m-%d")))
    for s, p in L["cortos"].items():
        try:
            c = motor.bolsa.contrato(s); px = motor.bolsa.precio_futuro(s)
        except Exception:
            c, px = dict(tam=p.get("tam", 1)), None
        noc = p["contratos"] * c["tam"] * (px or p["entrada"])
        out.append(dict(parte="Corto", simbolo=s, cantidad=p["contratos"], entrada=p["entrada"], precio=px, valor=-noc,
                        peso=-noc / T, resultado=(p["entrada"] / px - 1) if px else None, mitad=bool(p.get("reducida")),
                        desde=pd.to_datetime(p["ts"], unit="ms").strftime("%Y-%m-%d")))
    return out


def resumen_cerradas(db):
    f = db.filas("SELECT parte, pnl, pnl_pct FROM cerradas WHERE motivo NOT LIKE 'conflicto%'")
    out = {}
    for parte in ("A", "B"):
        x = [r for r in f if r["parte"] == parte]
        g = [r for r in x if (r["pnl"] or 0) > 0]
        out[parte] = dict(n=len(x), ganadoras=len(g) / len(x) if x else None, pnl=sum(r["pnl"] or 0 for r in x),
                          medio=float(np.mean([r["pnl_pct"] or 0 for r in x])) if x else None)
    return out


def ts(ms_):
    return pd.to_datetime(ms_, unit="ms").strftime("%Y-%m-%d %H:%M")


def ahora_ms():
    return int(time.time() * 1000)
