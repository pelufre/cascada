"""Evidencia para la segunda auditoría (2026-10-03). Diagnóstico: no cambia pesos, niveles ni reglas (protocolo §8).

1. Descomposición de la caída de rendimiento entre la 1.0 y la 1.2 en el MISMO período (2024-01-01 → 2026-09-30),
   corrida B (3000 USDT, contratos reales), cambiando una cosa por vez:
     A  1.0 declarada: modelo de investigación, pesos 1.0, spot            (de datos_backtest del commit 97824e0)
     B  motor validado, pesos 1.0, velas spot, sin funding, con costos     → A−B: modelo de investigación vs motor real
     C  motor validado, pesos 1.0, perpetuos + funding, con costos         → B−C: datos spot vs perpetuos y funding
     E  motor validado, pesos 1.0, perpetuos, sin costos ni funding        → E−C: costos y funding
     D  motor validado, pesos 1.2 congelados, perpetuos + funding, costos  → C−D: pesos (= la evaluación oficial)
2. Caída estricta (M4): los cuatro niveles congelados, en IS (2020–2023) y en la prueba (2024 → sep 2026), con el pico
   que incluye el mejor punto de cada vela.

    CASCADA_ABRIR_OOS=1 python -m validacion.auditoria2 --perp … --spot … --top50 … --resumen … --universo … --xbt …
"""
import argparse
import json
import subprocess
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from .correr_is import funding_xbt
from .datos import CONGELADO, CORTE_IS, FIN_OOS, INICIO_IS, Paquete, limite
from .motor_bt import Corrida, metricas

RAIZ = Path(__file__).resolve().parent
RES = RAIZ / "resultados" / "auditoria2"
PESOS_10 = {   # cascada/config.py del commit 97824e0 (versión 1.0 auditada)
    "20": dict(ab_cortos=0.426, mom_alts=0.319, balas5=0.191, rsi2_btc=0.084, wr2=0.180, sold_btc=0.107, rsi2_eth=0.096),
    "30": dict(ab_cortos=0.650, mom_alts=0.468, balas5=0.340, rsi2_btc=0.053, wr2=0.934, sold_btc=0.066, rsi2_eth=0.960),
}


def _correr(args):
    clave, pesos, datos, desde, hasta, extra, a = args
    p = Paquete(a.perp if datos == "perp" else a.spot)
    fx = funding_xbt(a.xbt, p, hasta) if datos == "perp" else None
    r = Corrida(p, a.top50, a.resumen, a.universo, pesos, modo="OOS", corrida="B", desde=desde, hasta=hasta,
                funding_xbt=fx, log_cada=0, **extra).correr()
    r.serie.to_pickle(RES / f"{clave}.pkl")
    m = metricas(r.serie)
    ops = r.operaciones()
    m.update(lotes=len(ops), funding=float(r.serie.funding.iat[-1]),
             comisiones=float(r.db.filas("SELECT COALESCE(SUM(comision),0) s FROM operaciones")[0]["s"]))
    return clave, m


def declarado_10(nivel):
    """Tasa y caída de la 1.0 entre 2024-01-01 y 2026-09-30 según su propia serie diaria publicada."""
    txt = subprocess.run(["git", "show", "97824e0:datos_backtest/cascada_niveles.csv"], cwd=RAIZ, capture_output=True,
                         text=True, check=True).stdout
    from io import StringIO
    e = pd.read_csv(StringIO(txt), index_col=0, parse_dates=True)[f"techo_intrabarra_{nivel}"]
    e = e[(e.index >= "2023-12-31") & (e.index < FIN_OOS)]
    años = (e.index[-1] - e.index[0]).days / 365.25
    return dict(cagr=float((e.iat[-1] / e.iat[0]) ** (1 / años) - 1), dd_optimista=float((e / e.cummax() - 1).min()),
                nota="serie diaria de la 1.0 (modelo de investigación, pesos elegidos con 2019–2026)")


def main(a):
    limite("OOS")
    RES.mkdir(parents=True, exist_ok=True)
    cong = json.loads(CONGELADO.read_text())
    T = []
    for n in ("20", "30"):
        T += [(f"B_n{n}_pesos10_spot", PESOS_10[n], "spot", CORTE_IS, FIN_OOS, {}, a),
              (f"C_n{n}_pesos10_perp", PESOS_10[n], "perp", CORTE_IS, FIN_OOS, {}, a),
              (f"E_n{n}_pesos10_perp_sin_costos", PESOS_10[n], "perp", CORTE_IS, FIN_OOS, dict(sin_costos=True, sin_funding=True), a)]
    for n, w in cong["pesos_por_nivel"].items():
        T += [(f"D_n{n}_oos", w, "perp", CORTE_IS, FIN_OOS, {}, a), (f"D_n{n}_is", w, "perp", INICIO_IS, CORTE_IS, {}, a)]
    out = {f"A_n{n}_declarado_1.0": declarado_10(n) for n in ("20", "30")}
    with ProcessPoolExecutor(a.procesos) as ex:
        for clave, m in ex.map(_correr, T):
            out[clave] = m
            print(f"{clave:32s} tasa {m['cagr']:7.1%}  caída optimista {m['dd_optimista']:6.1%}  pesimista "
                  f"{m['dd_pesimista']:6.1%}  estricta {m['dd_estricta']:6.1%}", flush=True)
    (RES / "resumen.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k in ("perp", "spot", "top50", "resumen", "universo", "xbt"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--procesos", type=int, default=2)
    main(ap.parse_args())
