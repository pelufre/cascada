"""Corridas de IS de cada estrategia sola (peso 1), corridas A y B (protocolo §5.1). Guarda las series en resultados/.

    python -m validacion.correr_is --datos CARPETA --top50 … --resumen … --universo … [--corrida A] [--solo rsi2_btc,…]
"""
import argparse
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from .datos import Paquete
from .motor_bt import PRIORIDAD, Corrida, metricas

SALIDA = Path(__file__).resolve().parent / "resultados"


def funding_xbt(ruta_xbt, paquete, hasta):
    """Funding de XBTUSDM (KuCoin) y, antes de que exista, el del perpetuo BTCUSDT del paquete."""
    partes = []
    F = paquete.funding(hasta)
    if F is not None and "BTC" in F:
        partes.append(F["BTC"].dropna())
    if ruta_xbt:
        x = pd.read_csv(ruta_xbt)
        x.index = pd.to_datetime(x["fecha_utc"], utc=True).dt.tz_localize(None)
        x = x["funding_rate"].astype(float)
        partes = [p[p.index < x.index[0]] for p in partes] + [x]
    if not partes:
        return None
    s = pd.concat(partes).sort_index()
    return s[s.index < pd.Timestamp(hasta)]


def uno(args):
    est, corrida, a = args
    p = Paquete(a["datos"])
    from .datos import CORTE_IS
    fx = funding_xbt(a.get("xbt"), p, CORTE_IS) if est == "balas5" else None
    r = Corrida(p, a["top50"], a["resumen"], a["universo"], {est: 1.0}, modo="IS", corrida=corrida,
                funding_xbt=fx, log_cada=0).correr()
    SALIDA.mkdir(exist_ok=True)
    pref = "dev_" if not p.perp else ""
    r.serie.to_pickle(SALIDA / f"{pref}is_{corrida}_{est}.pkl")
    m = metricas(r.serie)
    ops = r.operaciones()
    m.update(lotes=len(ops), segundos=r.info["segundos"],
             ganadoras=float((ops.pnl > 0).mean()) if len(ops) else None,
             factor_beneficio=float(ops.pnl[ops.pnl > 0].sum() / -ops.pnl[ops.pnl < 0].sum()) if len(ops) and (ops.pnl < 0).any() else None,
             peor_lote=float(ops.pnl.min()) if len(ops) else None, funding=float(r.serie.funding.iat[-1]))
    return est, corrida, m


def main(a):
    a = vars(a)
    ests = a["solo"].split(",") if a["solo"] else [e for e in PRIORIDAD if not e.startswith("hold_")]
    trabajos = [(e, c, a) for c in a["corrida"].split(",") for e in ests]
    out = {}
    with ProcessPoolExecutor(a["procesos"]) as ex:
        for est, corrida, m in ex.map(uno, trabajos):
            out[f"{corrida}_{est}"] = m
            print(f"{corrida} {est:10s} " + " ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}" for k, v in m.items()), flush=True)
    SALIDA.mkdir(exist_ok=True)
    pref = "dev_" if not Paquete(a["datos"], verificar=False).perp else ""
    (SALIDA / f"{pref}is_resumen.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k in ("datos", "top50", "resumen", "universo"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--xbt", default=None, help="funding_XBTUSDM.csv")
    ap.add_argument("--corrida", default="A"); ap.add_argument("--solo", default="")
    ap.add_argument("--procesos", type=int, default=2)
    main(ap.parse_args())
