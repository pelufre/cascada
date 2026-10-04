"""V10 (segunda auditoría): supuestos de datos que no se pueden certificar con huellas.

1. Proxy de 30 balas: velas del perpetuo BTCUSDT (Binance, el paquete) contra las de XBTUSDM (KuCoin, el contrato que se
   opera; velas de 1 h desde 2024-08). Diferencias de cierre, mínimo y máximo por vela de 4 h, y las velas donde el mínimo
   de XBTUSDM fue más bajo que el del proxy (lo que importa para la liquidación y la reserva).
2. Sensibilidad de 30 balas sola (corrida B, 1000 USD, contratos de 1 USD) en el tramo común: mismas reglas con las velas
   del proxy y con las de XBTUSDM (decisiones, reservas, liquidaciones, resultado y distancia mínima a la liquidación).
3. Colas del funding del paquete: los eventos más extremos, con símbolo, fecha e intervalo.

    python -m validacion.analisis_v10 --datos … --xbt_velas velas_XBTUSDM_60m.csv --xbt funding_XBTUSDM.csv
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from cascada.balas import Balas
from cascada.db import Base

from .correr_is import funding_xbt
from .datos import FIN_OOS, Paquete

RES = Path(__file__).resolve().parent / "resultados" / "auditoria3"
H4 = pd.Timedelta(hours=4)


def velas_xbt_4h(ruta):
    x = pd.read_csv(ruta)
    x.index = pd.to_datetime(x.fecha_utc, utc=True).dt.tz_localize(None)
    g = x.groupby(x.index.floor("4h"))
    v = pd.DataFrame(dict(o=g.open.first(), h=g.high.max(), l=g.low.min(), c=g.close.last(), n=g.size()))
    return v[v.n == 4].drop(columns="n"), len(x), x.index.min(), x.index.max()


def comparar_proxy(M, xbt):
    btc = pd.DataFrame({k: M[k]["BTC"] for k in ("o", "h", "l", "c")}).dropna()
    comunes = btc.index.intersection(xbt.index)
    b, x = btc.loc[comunes], xbt.loc[comunes]
    d = pd.DataFrame(dict(cierre=x.c / b.c - 1, minimo=x.l / b.l - 1, maximo=x.h / b.h - 1, apertura=x.o / b.o - 1))
    q = lambda s: {k: float(v) for k, v in dict(media=s.mean(), mediana=s.median(), p01=s.quantile(0.01), p99=s.quantile(0.99),
                                                 min=s.min(), max=s.max(), abs_max=s.abs().max()).items()}
    peores = d.minimo.nsmallest(10)
    return dict(velas_4h_comunes=len(comunes), desde=str(comunes.min()), hasta=str(comunes.max()),
                diferencias={k: q(d[k]) for k in d}, velas_minimo_xbt_mas_bajo_05pct=int((d.minimo < -0.005).sum()),
                velas_minimo_xbt_mas_bajo_1pct=int((d.minimo < -0.01).sum()),
                diez_minimos_mas_bajos_en_xbt={str(k): float(v) for k, v in peores.items()})


def correr_balas(velas, cierres_previos, fx, desde):
    """30 balas sola, paper, 1000 USD, contratos de 1 USD, sobre `velas` (4 h) desde `desde`."""
    bl = Balas(Base(":memory:"), 1000.0, contrato_usd=1.0)
    hist = pd.concat([cierres_previos[cierres_previos.index < desde], velas.c[velas.index >= desde]])
    dist = []; E = []; reservas = 0
    for t0, v in velas[velas.index >= desde].iterrows():
        t = t0 + H4
        ev = list(fx[(fx.index > t0) & (fx.index <= t)].items()) if fx is not None else []
        bl.vela(t, (v.o, v.h, v.l, v.c), ev)
        if bl.st["activo"] and bl.st.get("dist_liq") is not None:
            dist.append(bl.st["dist_liq"])
        resd = bl.st["resd"]
        cs = hist[hist.index <= t0].iloc[-400:]
        sig = velas.o.get(t, v.c)
        bl.decidir(t, cs, px=float(sig) if sig == sig else v.c)
        reservas += int(bl.st["resd"] and not resd)
        E.append((t, bl.patrimonio(v.c)))
    e = pd.Series(dict(E))
    return dict(campañas=bl.st["camps"], liquidaciones=bl.st["liqs"], reservas=reservas, final=float(e.iat[-1]),
                caida_max=float((e / e.cummax() - 1).min()), distancia_min_liquidacion=float(min(dist)) if dist else None,
                funding_usd=bl.st["funding_usd"], comisiones_usd=bl.st["comisiones_usd"])


def colas_funding(p):
    F = p.funding(FIN_OOS)
    s = F.stack().rename("tasa").reset_index()
    s.columns = ["fecha", "simbolo", "tasa"]
    inter = {}
    for b in F.columns:
        i = F[b].dropna().index.to_series().diff().dropna()
        inter[b] = i
    s["intervalo_h"] = [float(inter[r.simbolo].get(r.fecha, pd.Timedelta(0)) / pd.Timedelta(hours=1)) for r in s.itertuples()]
    fila = lambda r: dict(fecha=str(r.fecha), simbolo=r.simbolo, tasa=float(r.tasa), intervalo_h=r.intervalo_h)
    return dict(eventos=len(s), mas_negativos=[fila(r) for r in s.nsmallest(10, "tasa").itertuples()],
                mas_positivos=[fila(r) for r in s.nlargest(10, "tasa").itertuples()],
                eventos_abs_mayor_1pct=int((s.tasa.abs() > 0.01).sum()), eventos_abs_mayor_05pct=int((s.tasa.abs() > 0.005).sum()))


def main(a):
    RES.mkdir(parents=True, exist_ok=True)
    p = Paquete(a.datos)
    M = p.matrices(FIN_OOS)
    xbt, n1h, x0, x1 = velas_xbt_4h(a.xbt_velas)
    out = dict(archivo_xbt=dict(velas_1h=n1h, desde=str(x0), hasta=str(x1), velas_4h_completas=len(xbt)))
    out["proxy_vs_xbtusdm"] = comparar_proxy(M, xbt)
    fx = funding_xbt(a.xbt, p, FIN_OOS)
    btc = pd.DataFrame({k: M[k]["BTC"] for k in ("o", "h", "l", "c")}).dropna()
    desde = xbt.index.min() + pd.Timedelta(days=1)
    hibrida = btc.copy()
    hibrida.loc[xbt.index.intersection(btc.index)] = xbt.loc[xbt.index.intersection(btc.index), ["o", "h", "l", "c"]].values
    hasta = xbt.index.max()
    out["balas_sola_tramo_comun"] = dict(
        desde=str(desde), hasta=str(hasta), nota="velas de XBTUSDM donde hay 4 h completas; donde no, las del proxy",
        proxy_btcusdt=correr_balas(btc[btc.index <= hasta], btc.c, fx, desde),
        xbtusdm=correr_balas(hibrida[hibrida.index <= hasta], btc.c, fx, desde),
        velas_reemplazadas=int(len(xbt.index.intersection(btc.index[(btc.index >= desde) & (btc.index <= hasta)]))),
        velas_del_tramo=int(((btc.index >= desde) & (btc.index <= hasta)).sum()))
    out["funding_colas"] = colas_funding(p)
    (RES / "analisis_v10.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=float))
    print(json.dumps(out, indent=1, ensure_ascii=False, default=float))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--datos", required=True); ap.add_argument("--xbt_velas", required=True); ap.add_argument("--xbt", required=True)
    main(ap.parse_args())
