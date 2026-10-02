"""Protocolo §5.4: verifica los pesos elegidos con el motor completo sobre IS (corridas A y B).
Busca el mayor factor común que deja el percentil 95 de la caída (2000 remuestreos de los retornos diarios del motor) bajo el
nivel (secante log-log, corrida A) y después corre B con ese factor.

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


def correr(p, a, w, corrida, fx):
    r = Corrida(p, a.top50, a.resumen, a.universo, w, modo="IS", corrida=corrida, funding_xbt=fx, log_cada=0).correr()
    m = metricas(r.serie); m["p95"] = p95_motor(r.serie)
    ops = r.operaciones()
    m.update(lotes=len(ops), funding=float(r.serie.funding.iat[-1]),
             comisiones=float(r.db.filas("SELECT COALESCE(SUM(comision),0) s FROM operaciones")[0]["s"]))
    return r, m


def main(a):
    """Busca el mayor factor común f (≤ 1) con p95 del motor completo (corrida A) ≤ L: secante en log-log entre el
    último punto que cumple y el último que no, hasta 8 corridas o p95 ∈ [L − 1 pt, L]. Después corre B con ese f."""
    pref = "dev_" if a.dev else ""
    sel = json.loads((RES / f"{pref}seleccion_is.json").read_text())
    p = Paquete(a.datos)
    fx = funding_xbt(a.xbt, p, CORTE_IS)
    conocidos = json.loads(a.conocidos) if a.conocidos else {}
    for nivel in a.niveles.split(","):
        L = int(nivel) / 100
        w0 = dict(sel["niveles"][nivel]["pesos"])
        pts = {float(f): dict(p95=v) for f, v in conocidos.get(nivel, {}).items()}     # f -> métricas A
        f = 1.0
        for intento in range(8):
            if f not in pts:
                r, m = correr(p, a, {k: v * f for k, v in w0.items()}, "A", fx)
                pts[f] = dict(m, _r=r)
                print(f"nivel {nivel} f={f:.4f}: A tasa {m['cagr']:.1%} caída {m['dd_pesimista']:.1%} p95 {m['p95']:.1%}", flush=True)
            ok = [g for g in pts if pts[g]["p95"] <= L]; no = [g for g in pts if pts[g]["p95"] > L]
            lo = max(ok) if ok else None; hi = min(no) if no else None
            if hi is None or (lo is not None and pts[lo]["p95"] >= L - 0.01):
                break
            if lo is None:      # todavía nada cumple: secante con los dos menores que no cumplen (o potencia 1 si hay uno)
                g1, g2 = sorted(no)[:2] if len(no) > 1 else (hi, None)
                b = np.log(pts[g2]["p95"] / pts[g1]["p95"]) / np.log(g2 / g1) if g2 else 1.0
                b = min(max(b, 0.2), 2.0)
                f = g1 * (L * 0.995 / pts[g1]["p95"]) ** (1 / b)
            else:
                b = np.log(pts[hi]["p95"] / pts[lo]["p95"]) / np.log(hi / lo)
                f = lo * (L * 0.995 / pts[lo]["p95"]) ** (1 / max(b, 0.2))
                f = min(max(f, lo + 0.25 * (hi - lo)), hi - 0.1 * (hi - lo))
            f = round(float(f), 4)
        ok = [g for g in pts if pts[g]["p95"] <= L]
        f = max(ok) if ok else min(pts)
        w = {k: v * f for k, v in w0.items()}
        if "_r" in pts[f]:
            r, mA = pts[f]["_r"], {k: v for k, v in pts[f].items() if k != "_r"}
        else:
            r, mA = correr(p, a, w, "A", fx)
        r.serie.to_pickle(RES / f"{pref}is_motor_A_nivel{nivel}.pkl")
        r, mB = correr(p, a, w, "B", fx); r.serie.to_pickle(RES / f"{pref}is_motor_B_nivel{nivel}.pkl")
        print(f"nivel {nivel} FINAL f={f:.4f}: A tasa {mA['cagr']:.1%} caída {mA['dd_pesimista']:.1%} p95 {mA['p95']:.1%} | "
              f"B tasa {mB['cagr']:.1%} caída {mB['dd_pesimista']:.1%} p95 {mB['p95']:.1%}", flush=True)
        (RES / f"{pref}verificacion_{nivel}.json").write_text(json.dumps(
            dict(nivel=nivel, factor=f, pesos=w, resultados=dict(A=mA, B=mB), cumple=bool(mA["p95"] <= L),
                 busqueda={str(g): v["p95"] for g, v in sorted(pts.items())}), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k in ("datos", "top50", "resumen", "universo"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--xbt", default=None); ap.add_argument("--niveles", default="10,20,25,30"); ap.add_argument("--dev", action="store_true")
    ap.add_argument("--conocidos", default=None, help='JSON {nivel: {factor: p95}} de corridas A ya hechas con estos pesos')
    main(ap.parse_args())
