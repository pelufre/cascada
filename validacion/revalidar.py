"""Revalidación después de las correcciones de la segunda auditoría (enmienda E14): los MISMOS pesos congelados
(54ddf76), el motor corregido, IS 2020–2023 y prueba 2024-01-01 → 2026-09-30, corridas A y B. No elige nada: vuelve a
medir con el motor arreglado (protocolo §8: «se corrigen, se documentan y se vuelve a correr IS y OOS con los mismos
pesos»). Cada caso se guarda aparte (se puede cortar y retomar).

    CASCADA_ABRIR_OOS=1 python -m validacion.revalidar --datos … --top50 … --resumen … --universo … --xbt … [--procesos 2]
    CASCADA_ABRIR_OOS=1 python -m validacion.revalidar … --resumen_solo      # sólo arma el resumen con lo ya corrido

Casos: cartera de cada nivel (IS y prueba, A y B); en la prueba, corrida B: BTC tendencia (C3), BTC y ETH mantener
(perpetuo), pesos iguales por riesgo, sin balas y sin alts (nivel 30); variante «operable en KuCoin» (universo con
contrato del mismo ticker desde su fecha de apertura) de cada nivel, IS y prueba, corrida B.
"""
import argparse
import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from . import pesos as P
from .correr_is import funding_xbt
from .datos import CONGELADO, CORTE_IS, FIN_OOS, INICIO_IS, Paquete, limite
from .motor_bt import Corrida, metricas, por_año
from .verificar_motor import p95_motor

RAIZ = Path(__file__).resolve().parent
RES = RAIZ / "resultados" / "revalidacion"
TRAMOS = {"IS": (INICIO_IS, CORTE_IS, "IS"), "OOS": (CORTE_IS, FIN_OOS, "OOS")}


def _stats_lotes(rr):
    """Lotes de la principal netos del funding que pagó cada uno (cerrados y abiertos por separado) y campañas de balas."""
    ops = rr.operaciones()
    out = {}
    if len(ops):
        f = rr.info.get("funding_por_lote", {})
        ops["neto"] = ops.pnl.fillna(0.0) - ops.id.map(f).fillna(0.0)
        c = ops[ops.cerrado_ts.notna()]
        out.update(lotes=len(ops), lotes_cerrados=len(c), lotes_abiertos=int(ops.cerrado_ts.isna().sum()),
                   ganadoras=float((c.neto > 0).mean()) if len(c) else None,
                   factor_beneficio=float(c.neto[c.neto > 0].sum() / max(-c.neto[c.neto < 0].sum(), 1e-9)) if len(c) else None,
                   peor_lote=float(c.neto.min()) if len(c) else None)
    camp = pd.DataFrame(rr.info.get("campañas_balas") or [])
    if len(camp):
        out["campañas_balas"] = dict(n=len(camp), ganadoras=float((camp.resultado > 0).mean()),
                                     factor_beneficio=float(camp.resultado[camp.resultado > 0].sum() /
                                                            max(-camp.resultado[camp.resultado < 0].sum(), 1e-9)),
                                     peor=float(camp.resultado.min()), liquidaciones=int((camp.motivo != "salida").sum()))
    return out


def _caso(args):
    clave, pesos, corrida, tramo, operable, a = args
    salida = RES / f"{clave}.json"
    if salida.exists():
        return clave, json.loads(salida.read_text()), 0
    t0 = time.time()
    desde, hasta, modo = TRAMOS[tramo]
    p = Paquete(a.datos)
    fx = funding_xbt(a.xbt, p, hasta)
    rr = Corrida(p, a.top50, a.resumen, a.universo, pesos, modo=modo, corrida=corrida, desde=desde, hasta=hasta,
                 funding_xbt=fx, log_cada=0, operable=operable).correr()
    m = metricas(rr.serie)
    m["p95"] = p95_motor(rr.serie)
    m["por_año"] = por_año(rr.serie)
    m.update(_stats_lotes(rr))
    at = rr.info["atribucion"]
    m["atribucion"] = at
    m["funding"] = at["funding_principal"] + (at.get("balas", {}).get("funding") or 0.0)
    m["comisiones"] = at["comisiones_principal"] + (at.get("balas", {}).get("comisiones") or 0.0)
    m.update(pesos=pesos, corrida=corrida, tramo=tramo, operable=operable, segundos=round(time.time() - t0))
    rr.serie.to_pickle(RES / f"{clave}.pkl")
    salida.write_text(json.dumps(m, indent=1, ensure_ascii=False, default=float))
    return clave, m, time.time() - t0


def trabajos(a, cong):
    from .evaluar_oos import igual_riesgo
    W = cong["pesos_por_nivel"]
    var = cong["variante_balas"]
    T = []
    orden = ["30", "20", "10", "25"]
    for n in orden:                                  # lo que deciden los criterios primero (B), después A
        T += [(f"n{n}_IS_B", W[n], "B", "IS", False), (f"n{n}_OOS_B", W[n], "B", "OOS", False)]
        if n == "30":
            T += [("btc_tendencia_OOS_B", {"btc_tend": 1.0}, "B", "OOS", False)]
    w30 = W["30"]
    T += [("btc_mantener_OOS_B", {"hold_btc": 1.0}, "B", "OOS", False), ("eth_mantener_OOS_B", {"hold_eth": 1.0}, "B", "OOS", False),
          ("igual_riesgo_OOS_B", igual_riesgo(w30, 0.30), "B", "OOS", False),
          ("sin_balas_OOS_B", {k: (0.0 if k == var else v) for k, v in w30.items()}, "B", "OOS", False),
          ("sin_alts_OOS_B", {k: (0.0 if k in ("ab_cortos", "mom_alts") else v) for k, v in w30.items()}, "B", "OOS", False)]
    for n in orden:
        T += [(f"n{n}_IS_A", W[n], "A", "IS", False), (f"n{n}_OOS_A", W[n], "A", "OOS", False)]
    for n in orden:
        T += [(f"n{n}_IS_B_operable", W[n], "B", "IS", True), (f"n{n}_OOS_B_operable", W[n], "B", "OOS", True)]
    return [t + (a,) for t in T]


def main(a):
    limite("OOS")
    RES.mkdir(parents=True, exist_ok=True)
    cong = json.loads(CONGELADO.read_text())
    if not a.resumen_solo:
        T = trabajos(a, cong)
        if a.solo:
            T = [t for t in T if any(t[0].startswith(x) for x in a.solo.split(","))]
        with ProcessPoolExecutor(a.procesos) as ex:
            for clave, m, seg in ex.map(_caso, T):
                print(f"{time.strftime('%H:%M:%S')} {clave:24s} tasa {m['cagr']:7.1%}  caída pes {m['dd_pesimista']:6.1%}  "
                      f"estricta {m['dd_estricta']:6.1%}  p95 {m['p95']:5.1%}  ({seg:.0f} s)", flush=True)
    from .resumen_revalidacion import resumir
    resumir(cong, a)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k in ("datos", "top50", "resumen", "universo", "xbt"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--procesos", type=int, default=2)
    ap.add_argument("--solo", default="", help="prefijos de casos a correr (ej. n30_,btc_)")
    ap.add_argument("--resumen_solo", action="store_true")
    main(ap.parse_args())
