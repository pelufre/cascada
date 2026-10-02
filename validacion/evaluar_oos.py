"""Protocolo §6–7: abre el período de prueba 2024-01-01 → 2026-09-30 UNA vez, con los pesos congelados.

    CASCADA_ABRIR_OOS=1 python -m validacion.evaluar_oos --datos … --top50 … --resumen … --universo … --xbt …
"""
import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import pesos as P
from .correr_is import funding_xbt
from .datos import CONGELADO, CORTE_IS, FIN_OOS, Paquete, limite
from .motor_bt import Corrida, metricas
from .verificar_motor import p95_motor

RAIZ = Path(__file__).resolve().parent
RES = RAIZ / "resultados"


def por_año(serie):
    e = serie.E.resample("YE").last()
    e0 = pd.concat([pd.Series([serie.E.iat[0]], [serie.index[0]]), e]).values
    return {str(i.year): float(v / v0 - 1) for i, v, v0 in zip(e.index, e.values, e0[:-1])}


def main(a):
    limite("OOS")                                       # falla si los pesos no están congelados en un commit
    cong = json.loads(CONGELADO.read_text())
    commit = subprocess.run(["git", "log", "-1", "--format=%H", "--", str(CONGELADO)], cwd=RAIZ, capture_output=True, text=True).stdout.strip()
    nivel = cong["nivel_elegido"]; L = int(nivel) / 100
    w = cong["pesos_por_nivel"][nivel]
    var = cong["variante_balas"]
    sel = json.loads((RES / "seleccion_is.json").read_text())
    p95_is = sel.get("verificacion_motor", {}).get(nivel, {}).get("resultados", {}).get("B", {}).get("p95") \
        or sel["niveles"][nivel]["p95_is"]
    p = Paquete(a.datos)
    fx = funding_xbt(a.xbt, p, FIN_OOS)
    # pesos iguales por riesgo (inversa de la volatilidad diaria de IS), escalados al mismo nivel con el modelo rápido
    nombres = list(w)
    rutas = {k: RES / f"is_A_{k}.pkl" for k in nombres}
    idx, nom, r, rp, x = P.series(rutas)
    d = P.preparar(idx, r, rp, x)
    vol = np.array([pd.Series(r[:, j], idx).add(1).resample("D").prod().sub(1).std() for j in range(len(nom))])
    base = np.where(np.array([w[k] for k in nom]) > 0, 1 / np.maximum(vol, 1e-9), 0); base = base / base.sum()
    s = P._escala_max(base, L, d, P.remuestreos(len(d[4]), 2000))
    igual = {k: float(v) for k, v in zip(nom, P.a_grilla(base * s))}
    casos = {
        "cartera": w,
        "btc_mantener": {"hold_btc": 1.0}, "eth_mantener": {"hold_eth": 1.0}, "btc_tendencia": {"btc_tend": 1.0},
        "igual_riesgo": igual,
        "sin_balas": {k: (0.0 if k == var else v) for k, v in w.items()},
        "sin_alts": {k: (0.0 if k in ("ab_cortos", "mom_alts") else v) for k, v in w.items()},
    }
    out = dict(protocolo_commit_pesos=commit, nivel=nivel, variante=var, tramo=f"{CORTE_IS.date()} → {FIN_OOS.date()} (excl.)",
               ejecutado=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), casos={})
    for nombre, pesos in casos.items():
        for corrida in (("A", "B") if nombre == "cartera" else ("B",)):
            rr = Corrida(p, a.top50, a.resumen, a.universo, pesos, modo="OOS", corrida=corrida, desde=CORTE_IS, hasta=FIN_OOS,
                         funding_xbt=fx, log_cada=0).correr()
            m = metricas(rr.serie); m["p95"] = p95_motor(rr.serie); m["por_año"] = por_año(rr.serie)
            ops = rr.operaciones()
            if len(ops):
                m.update(lotes=len(ops), ganadoras=float((ops.pnl > 0).mean()), peor_lote=float(ops.pnl.min()),
                         factor_beneficio=float(ops.pnl[ops.pnl > 0].sum() / max(-ops.pnl[ops.pnl < 0].sum(), 1e-9)),
                         por_estrategia={k: float(v) for k, v in ops.groupby("estrategia").pnl.sum().items()})
            m.update(funding=float(rr.serie.funding.iat[-1]),
                     comisiones=float(rr.db.filas("SELECT COALESCE(SUM(comision),0) s FROM operaciones")[0]["s"]), pesos=pesos)
            out["casos"][f"{nombre}_{corrida}"] = m
            rr.serie.to_pickle(RES / f"oos_{nombre}_{corrida}.pkl")
            print(f"{nombre:13s} {corrida}: tasa {m['cagr']:.1%}  caída {m['dd_pesimista']:.1%}  Calmar {m['calmar'] or 0:.2f}", flush=True)
    c, bt = out["casos"]["cartera_B"], out["casos"]["btc_tendencia_B"]
    out["criterios"] = dict(
        C1=dict(ok=c["cagr"] > 0, valor=c["cagr"]),
        C2=dict(ok=-c["dd_pesimista"] <= p95_is, valor=c["dd_pesimista"], limite=-p95_is),
        C3=dict(ok=(c["calmar"] or -9) >= (bt["calmar"] or -9), cartera=c["calmar"], btc_tendencia=bt["calmar"]))
    out["aprobada"] = all(v["ok"] for v in out["criterios"].values())
    texto = json.dumps(out, indent=1, ensure_ascii=False, default=float)
    (RES / "resultado_oos.json").write_text(texto)
    h = hashlib.sha256(texto.encode()).hexdigest()
    with open(RAIZ / "REGISTRO.md", "a") as f:
        f.write(f"| {time.strftime('%Y-%m-%d')} | Evaluación OOS (pesos {commit[:7]}): {'APROBADA' if out['aprobada'] else 'NO aprobada'} | resultado_oos.json sha256 {h[:16]} |\n")
    print("\nCriterios:", {k: v["ok"] for k, v in out["criterios"].items()}, "→", "APROBADA" if out["aprobada"] else "NO APROBADA")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k in ("datos", "top50", "resumen", "universo"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--xbt", default=None)
    main(ap.parse_args())
