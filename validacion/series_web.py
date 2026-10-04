"""Series del tablero web (datos_backtest/) con el motor 1.3.1 (E14 y E15) y los pesos congelados.

Corre de corrido 2020-01-01 → 2026-09-30 cada nivel congelado (corrida B: 3000 USDT con contratos reales) y cada
estrategia sola (corrida A, peso 1). Sólo usa pesos congelados en 54ddf76, así que no elige nada (protocolo §8).

    CASCADA_ABRIR_OOS=1 python -m validacion.series_web --datos … --top50 … --resumen … --universo … --xbt … [--solo nivel_30,sola_mom_alts]
"""
import argparse
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from .correr_is import funding_xbt
from .datos import CONGELADO, CORTE_IS, FIN_OOS, INICIO_IS, Paquete, limite
from .motor_bt import Corrida, metricas, por_año

RAIZ = Path(__file__).resolve().parent
RES = RAIZ / "resultados"
WEB = RAIZ.parent / "datos_backtest"
ESTRATEGIAS = ["ab_cortos", "mom_alts", "balas5", "rsi2_btc", "wr2", "sold_btc", "rsi2_eth"]


def _correr(args):
    clave, pesos, corrida, a = args
    p = Paquete(a.datos)
    fx = funding_xbt(a.xbt, p, FIN_OOS)
    r = Corrida(p, a.top50, a.resumen, a.universo, pesos, modo="OOS", corrida=corrida, desde=INICIO_IS, hasta=FIN_OOS,
                funding_xbt=fx, log_cada=0).correr()
    r.serie.to_pickle(RES / f"completo_{clave}.pkl")
    return clave


def resumen(serie):
    m = metricas(serie)
    is_ = metricas(serie, hasta=CORTE_IS); oos = metricas(serie, desde=CORTE_IS)
    return dict(cagr=m["cagr"], dd_diaria=m["dd_optimista"], dd_intrabarra=m["dd_pesimista"], dd_estricta=m["dd_estricta"],
                sharpe=m["sharpe"], anual=por_año(serie), cagr_is=is_["cagr"], dd_is=is_["dd_pesimista"],
                cagr_oos=oos["cagr"], dd_oos=oos["dd_pesimista"], nocional_max=m.get("nocional_max"),
                exposicion_max=m.get("exposicion_max"))


def main(a):
    limite("OOS")
    cong = json.loads(CONGELADO.read_text())
    trabajos = [(f"nivel_{n}", w, "B", a) for n, w in sorted(cong["pesos_por_nivel"].items(), key=lambda x: -int(x[0]))]
    trabajos += [(f"sola_{k}", {k: 1.0}, "A", a) for k in ESTRATEGIAS]
    if a.solo:                                       # sólo rehace estas curvas; las demás se leen de lo guardado
        trabajos = [t for t in trabajos if t[0] in a.solo.split(",")]
    if not a.solo_exportar:
        with ProcessPoolExecutor(a.procesos) as ex:
            for c in ex.map(_correr, trabajos):
                print("listo", c, flush=True)
    dia = lambda s: s.E.resample("D").last().dropna()
    niv = {f"nivel_{n}": pd.read_pickle(RES / f"completo_nivel_{n}.pkl") for n in cong["pesos_por_nivel"]}
    sol = {k: pd.read_pickle(RES / f"completo_sola_{k}.pkl") for k in ESTRATEGIAS}
    WEB.mkdir(exist_ok=True)
    N = pd.DataFrame({k: dia(s) / s.E.iat[0] for k, s in niv.items()}); N.index.name = "fecha"
    S = pd.DataFrame({k: dia(s) / s.E.iat[0] for k, s in sol.items()}); S.index.name = "fecha"
    N.round(6).to_csv(WEB / "cascada_niveles.csv"); S.round(6).to_csv(WEB / "estrategias.csv")
    out = {k: resumen(s) for k, s in niv.items()}
    out["_info"] = dict(fuente="validacion/series_web.py, motor 1.3.1 (enmiendas E14 y E15), pesos congelados 54ddf76",
                        ajuste="2020-01-01 → 2023-12-31", prueba="2024-01-01 → 2026-09-30", corrida="B (3000 USDT)")
    (WEB / "resumen.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    for k, v in out.items():
        if not k.startswith("_"):
            print(k, f"tasa {v['cagr']:.1%} caída {v['dd_intrabarra']:.1%} | IS {v['cagr_is']:.1%} OOS {v['cagr_oos']:.1%}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k in ("datos", "top50", "resumen", "universo"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--xbt", default=None); ap.add_argument("--procesos", type=int, default=2)
    ap.add_argument("--solo_exportar", action="store_true")
    ap.add_argument("--solo", default="", help="claves a rehacer (ej. nivel_30,sola_mom_alts)")
    main(ap.parse_args())
