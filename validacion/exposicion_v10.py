"""V10 con los lotes del nivel 30 (corrida B, motor 1.3; IS y prueba, de validacion.auditoria3):
  1. lotes en símbolos sin contrato USDT-M del mismo ticker en KuCoin (lo que el servicio no podría operar);
  2. lotes abiertos durante los huecos de datos de 2022 (E9): movimiento entre el último cierre antes del hueco y la
     primera apertura después, y si el stop se ejecutó al reaparecer;
  3. lotes expuestos a los eventos de funding más extremos del paquete.

    python -m validacion.exposicion_v10 --datos …
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .datos import FIN_OOS, Paquete

RES = Path(__file__).resolve().parent / "resultados" / "auditoria3"


def _lotes():
    out = []
    for tramo, f in (("IS", "D_n30_is_lotes"), ("prueba", "D_n30_oos_lotes")):
        p = RES / f"{f}_lotes.pkl"
        if p.exists():
            d = pd.read_pickle(p)
            d["tramo"] = tramo
            ops = RES / f"{f}_ops.pkl"
            if ops.exists():                       # nocional de entrada de cada lote (primera operación)
                o = pd.read_pickle(ops)
                o = o[o.cuenta == "principal"].sort_values("id")
                d["noc_entrada"] = d.id.map(o.groupby("lote").nocional.first())
            out.append(d)
    L = pd.concat(out, ignore_index=True)
    L["abre"] = pd.to_datetime(L.abierto_ts, unit="ms"); L["cierra"] = pd.to_datetime(L.cerrado_ts, unit="ms")
    if "noc_entrada" not in L:
        L["noc_entrada"] = np.nan
    return L


def main(a):
    p = Paquete(a.datos)
    M = p.matrices(FIN_OOS)
    K = p.contratos()
    L = _lotes()
    out = {}
    # 1) sin contrato del mismo ticker
    sin = L[~L.simbolo.isin(K.index)]
    out["sin_contrato_kucoin"] = dict(
        lotes=int(len(sin)), pnl_total=float(sin.pnl.sum()),
        por_simbolo={s: dict(lotes=int(len(g)), pnl=float(g.pnl.sum()), tramos=sorted(g.tramo.unique()),
                             estrategias=sorted(g.estrategia.unique())) for s, g in sin.groupby("simbolo")},
        nota="pnl neto de comisiones, sin funding; quitar el símbolo cambia también señales y cupos (ver variante operable)")
    # 2) huecos de 2022 (E9)
    C = M["c"]; O = M["o"]
    huecos = []
    for ini, fin in (("2022-02-26", "2022-03-01"), ("2022-04-01", "2022-04-03")):
        ventana = C.loc[ini:fin]
        faltan = [s for s in ventana.columns if ventana[s].isna().any() and C[s].loc[:ini].notna().any()
                  and C[s].loc[fin:].notna().any()]
        for s in faltan:
            na = ventana[s][ventana[s].isna()]
            t0, t1 = na.index.min(), na.index.max()
            antes = C[s].loc[:t0].dropna()
            despues = O[s].loc[t1:].dropna()
            if not len(antes) or not len(despues):
                continue
            c0, o1 = float(antes.iat[-1]), float(despues.iat[0])
            exp = L[(L.simbolo == s) & (L.abre <= t0) & ((L.cierra.isna()) | (L.cierra > t0))]
            for r in exp.itertuples():
                noc = r.noc_entrada if r.noc_entrada == r.noc_entrada else None
                huecos.append(dict(simbolo=s, desde=str(t0), hasta=str(t1), tramo=r.tramo, estrategia=r.estrategia,
                                   lado=int(r.lado), nocional_entrada=noc, movimiento_hueco=o1 / c0 - 1,
                                   efecto_aprox=float(r.lado * (o1 / c0 - 1) * noc) if noc else None,
                                   resultado_lote=float(r.pnl or 0), salida=r.motivo_salida, cerro=str(r.cierra)))
    out["huecos_2022"] = dict(lotes_expuestos=len(huecos), detalle=huecos,
                              peor_movimiento_en_contra=float(min([h["lado"] * h["movimiento_hueco"] for h in huecos]))
                              if huecos else None)
    # 3) funding extremo
    F = p.funding(FIN_OOS)
    s = F.stack().rename("tasa").reset_index(); s.columns = ["fecha", "simbolo", "tasa"]
    ext = s[s.tasa.abs() >= 0.01]
    expuestos = []
    for r in ext.itertuples():
        g = L[(L.simbolo == r.simbolo) & (L.abre < r.fecha) & ((L.cierra.isna()) | (L.cierra >= r.fecha))]
        for x in g.itertuples():
            noc = x.noc_entrada if x.noc_entrada == x.noc_entrada else None
            expuestos.append(dict(fecha=str(r.fecha), simbolo=r.simbolo, tasa=float(r.tasa), estrategia=x.estrategia,
                                  lado=int(x.lado), nocional_entrada=noc,
                                  pago_aprox=float(x.lado * r.tasa * noc) if noc else None))
    out["funding_extremo"] = dict(eventos_abs_mayor_1pct=int(len(ext)), lotes_expuestos=len(expuestos), detalle=expuestos[:60],
                                  pagado_aprox_total=float(sum(e["pago_aprox"] or 0 for e in expuestos)))
    (RES / "exposicion_v10.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=float))
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "detalle"} for k, v in out.items()}, indent=1,
                     ensure_ascii=False, default=float))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--datos", required=True)
    main(ap.parse_args())
