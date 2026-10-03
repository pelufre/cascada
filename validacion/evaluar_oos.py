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
from .cartera_lotes import cargar
from .motor_bt import Corrida, metricas
from .verificar_motor import p95_motor

RAIZ = Path(__file__).resolve().parent
RES = RAIZ / "resultados"


def por_año(serie):
    e = serie.E.resample("YE").last()
    e0 = pd.concat([pd.Series([serie.E.iat[0]], [serie.index[0]]), e]).values
    return {str(i.year): float(v / v0 - 1) for i, v, v0 in zip(e.index, e.values, e0[:-1])}


def _caso(args):
    nombre, pesos, corrida, a, desde, hasta, modo, guardar = args
    p = Paquete(a.datos)
    fx = funding_xbt(a.xbt, p, hasta)
    rr = Corrida(p, a.top50, a.resumen, a.universo, pesos, modo=modo, corrida=corrida, desde=desde, hasta=hasta,
                 funding_xbt=fx, log_cada=0).correr()
    m = metricas(rr.serie); m["p95"] = p95_motor(rr.serie); m["por_año"] = por_año(rr.serie)
    ops = rr.operaciones()
    if len(ops):
        m.update(lotes=len(ops), ganadoras=float((ops.pnl > 0).mean()), peor_lote=float(ops.pnl.min()),
                 factor_beneficio=float(ops.pnl[ops.pnl > 0].sum() / max(-ops.pnl[ops.pnl < 0].sum(), 1e-9)),
                 por_estrategia={k: float(v) for k, v in ops.groupby("estrategia").pnl.sum().items()})
    m.update(funding=float(rr.serie.funding.iat[-1]),
             comisiones=float(rr.db.filas("SELECT COALESCE(SUM(comision),0) s FROM operaciones")[0]["s"]), pesos=pesos)
    if guardar:
        rr.serie.to_pickle(RES / f"oos_{nombre}_{corrida}.pkl")
    return f"{nombre}_{corrida}", m


def igual_riesgo(w, L):
    """Pesos iguales por riesgo (inversa de la volatilidad diaria de IS de cada estrategia sola), con el mayor factor común
    que deja el p95 (2000 remuestreos, cartera por lotes) ≤ L. Sólo usa IS."""
    nombres = list(w)
    cart = cargar({k: RES / f"is_A_{k}.pkl" for k in nombres}, {k: RES / f"is_A_{k}_lotes.pkl" for k in nombres}, nombres)
    vol = []
    for k in nombres:
        e = pd.read_pickle(RES / f"is_A_{k}.pkl").E
        vol.append(e.resample("D").last().pct_change().std())
    base = np.where(np.array([w[k] for k in nombres]) > 0, 1 / np.maximum(np.array(vol), 1e-9), 0.0)
    base = base / base.sum()
    R = P.remuestreos(len(cart.inicios), 2000)
    fs = np.linspace(1.0 / base.max(), 0.01, 200)
    _, p95 = P.evaluar_lotes(base[None, :] * fs[:, None], cart, R)
    f = fs[np.argmax(p95 <= L)] if np.any(p95 <= L) else fs[-1]
    return {k: float(v) for k, v in zip(nombres, base * f)}


def main(a):
    from concurrent.futures import ProcessPoolExecutor
    if a.prueba:            # ensayo del script sobre IS (2023), sin abrir el OOS ni escribir resultados
        cong = json.loads(a.prueba_pesos.read_text()) if a.prueba_pesos else None
        desde, hasta, modo = pd.Timestamp("2023-01-01"), CORTE_IS, "IS"
        commit = "prueba"
    else:
        limite("OOS")                                   # falla si los pesos no están congelados en un commit
        cong = json.loads(CONGELADO.read_text())
        commit = subprocess.run(["git", "log", "-1", "--format=%H", "--", str(CONGELADO)], cwd=RAIZ, capture_output=True,
                                text=True).stdout.strip()
        desde, hasta, modo = CORTE_IS, FIN_OOS, "OOS"
    nivel = a.nivel or cong["nivel_elegido"]; L = int(nivel) / 100
    informativo = nivel != cong["nivel_elegido"]       # E11: otro nivel congelado, no cambia la decisión
    w = cong["pesos_por_nivel"][nivel]
    var = cong["variante_balas"]
    p95_is = cong["verificacion_motor"][nivel]["resultados"]["B"]["p95"]      # §7 C2: motor completo, corrida B, IS
    casos = {"cartera": w} if informativo else {
        "cartera": w,
        "btc_mantener": {"hold_btc": 1.0}, "eth_mantener": {"hold_eth": 1.0}, "btc_tendencia": {"btc_tend": 1.0},
        "igual_riesgo": igual_riesgo(w, L),
        "sin_balas": {k: (0.0 if k == var else v) for k, v in w.items()},
        "sin_alts": {k: (0.0 if k in ("ab_cortos", "mom_alts") else v) for k, v in w.items()},
    }
    out = dict(protocolo_commit_pesos=commit, nivel=nivel, variante=var, p95_is_B=p95_is,
               tramo=f"{desde.date()} → {hasta.date()} (excl.)",
               ejecutado=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), casos={})
    sufijo = f"_nivel{nivel}" if informativo else ""
    trabajos = [(n + sufijo, pw, c, a, desde, hasta, modo, not a.prueba) for n, pw in casos.items()
                for c in (("A", "B") if n == "cartera" else ("B",))]
    with ProcessPoolExecutor(a.procesos) as ex:
        for clave, m in ex.map(_caso, trabajos):
            out["casos"][clave] = m
            print(f"{clave:15s}: tasa {m['cagr']:.1%}  caída {m['dd_pesimista']:.1%}  Calmar {m['calmar'] or 0:.2f}", flush=True)
    c = out["casos"][f"cartera{sufijo}_B"]
    bt = out["casos"]["btc_tendencia_B"] if not informativo else \
        json.loads((RES / "resultado_oos.json").read_text())["casos"]["btc_tendencia_B"]
    out["criterios"] = dict(
        C1=dict(ok=c["cagr"] > 0, valor=c["cagr"]),
        C2=dict(ok=-c["dd_pesimista"] <= p95_is, valor=c["dd_pesimista"], limite=-p95_is),
        C3=dict(ok=(c["calmar"] or -9) >= (bt["calmar"] or -9), cartera=c["calmar"], btc_tendencia=bt["calmar"]))
    out["aprobada"] = all(v["ok"] for v in out["criterios"].values())
    texto = json.dumps(out, indent=1, ensure_ascii=False, default=float)
    if a.prueba:
        print("\n(prueba sobre IS) Criterios:", {k: v["ok"] for k, v in out["criterios"].items()})
        return
    salida = RES / (f"resultado_oos_nivel{nivel}.json" if informativo else "resultado_oos.json")
    salida.write_text(texto)
    h = hashlib.sha256(texto.encode()).hexdigest()
    with open(RAIZ / "REGISTRO.md", "a") as f:
        f.write(f"| {time.strftime('%Y-%m-%d')} | Evaluación OOS{' informativa' if informativo else ''} nivel {nivel} (pesos {commit[:7]}): {'APROBADA' if out['aprobada'] else 'NO aprobada'} | {salida.name} sha256 {h[:16]} |\n")
    print("\nCriterios:", {k: v["ok"] for k, v in out["criterios"].items()}, "→", "APROBADA" if out["aprobada"] else "NO APROBADA")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k in ("datos", "top50", "resumen", "universo"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--xbt", default=None); ap.add_argument("--procesos", type=int, default=2)
    ap.add_argument("--nivel", default=None, help="E11: evaluar otro nivel congelado (informativo, no cambia la decisión)")
    ap.add_argument("--prueba", action="store_true", help="ensayo sobre IS 2023 (no abre el OOS)")
    ap.add_argument("--prueba_pesos", type=Path, default=None, help="json con el formato de pesos_congelados (para --prueba)")
    main(ap.parse_args())
