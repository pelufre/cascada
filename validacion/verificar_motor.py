"""Protocolo §5.4: verifica los pesos elegidos con el motor completo sobre IS (corridas A y B).
Si el percentil 95 de la caída (2000 remuestreos de los retornos diarios del motor) supera el nivel, escala todos los
pesos por igual y vuelve a correr (hasta 3 veces).

    python -m validacion.verificar_motor --datos … --top50 … --resumen … --universo … --xbt … [--niveles 20] [--dev]
"""
import argparse
import json
from pathlib import Path

import numpy as np

from . import pesos as P
from .correr_is import funding_xbt
from .datos import CORTE_IS, Paquete
from .motor_bt import Corrida, metricas

RES = Path(__file__).resolve().parent / "resultados"


def p95_motor(serie, n=2000):
    lr = np.log(serie.E.resample("D").last().dropna()).diff().dropna().values
    return P.caida_p95(lr, P.remuestreos(len(lr), n))


def main(a):
    pref = "dev_" if a.dev else ""
    sel = json.loads((RES / f"{pref}seleccion_is.json").read_text())
    p = Paquete(a.datos)
    fx = funding_xbt(a.xbt, p, CORTE_IS)
    ver = sel.setdefault("verificacion_motor", {})
    for nivel in a.niveles.split(","):
        L = int(nivel) / 100
        w = dict(sel["niveles"][nivel]["pesos"])
        for intento in range(4):
            res = {}
            for corrida in ("A", "B"):
                r = Corrida(p, a.top50, a.resumen, a.universo, w, modo="IS", corrida=corrida, funding_xbt=fx, log_cada=0).correr()
                m = metricas(r.serie); m["p95"] = p95_motor(r.serie)
                ops = r.operaciones()
                m.update(lotes=len(ops), funding=float(r.serie.funding.iat[-1]),
                         comisiones=float(r.db.filas("SELECT COALESCE(SUM(comision),0) s FROM operaciones")[0]["s"]))
                res[corrida] = m
                r.serie.to_pickle(RES / f"{pref}is_motor_{corrida}_nivel{nivel}.pkl")
            print(f"nivel {nivel} intento {intento}: " + " | ".join(
                f"{c}: tasa {m['cagr']:.1%} caída {m['dd_pesimista']:.1%} p95 {m['p95']:.1%}" for c, m in res.items()), flush=True)
            if res["A"]["p95"] <= L or intento == 3:
                break
            f = max(0.5, min(0.98, L / res["A"]["p95"]))
            w = {k: float(P.a_grilla(v * f)) for k, v in w.items()}
        ver[nivel] = dict(pesos=w, resultados=res, intentos=intento + 1)
    (RES / f"{pref}seleccion_is.json").write_text(json.dumps(sel, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k in ("datos", "top50", "resumen", "universo"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--xbt", default=None); ap.add_argument("--niveles", default="10,20,25,30"); ap.add_argument("--dev", action="store_true")
    main(ap.parse_args())
