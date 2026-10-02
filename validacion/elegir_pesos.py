"""Protocolo §5: decisión de 30 balas, pesos por nivel con IS y chequeo con 2000 remuestreos.

    python -m validacion.elegir_pesos [--dev]

Lee las corridas de cada estrategia sola (resultados/is_A_*.pkl) y escribe resultados/seleccion_is.json.
"""
import argparse
import json
from pathlib import Path

import numpy as np

from . import pesos as P

RES = Path(__file__).resolve().parent / "resultados"
NIVELES = [0.10, 0.20, 0.25, 0.30]
BASE = ["ab_cortos", "mom_alts", "{bal}", "rsi2_btc", "wr2", "sold_btc", "rsi2_eth"]


def datos(variante, pref):
    nombres = [n.format(bal=variante) for n in BASE]
    idx, nom, r, rp, x = P.series({k: RES / f"{pref}is_A_{k}.pkl" for k in nombres})
    return nom, P.preparar(idx, r, rp, x)


def main(a):
    pref = "dev_" if a.dev else ""
    out = dict(protocolo="validacion/PROTOCOLO.md §5", corrida="A", tramo="IS 2019-01-01 → 2023-12-31")
    # §5.5: balas contra BTC tendencia en el nivel 20 %
    var = {}
    for v in ("balas5", "btc_tend"):
        nom, d = datos(v, pref)
        w, c, p = P.buscar(d, 0.20, n_dir=a.n_dir)
        var[v] = dict(pesos=dict(zip(nom, map(float, w))), tasa=float(c), p95=float(p))
        print(f"{v:9s} nivel 20 %: tasa IS {c:.1%}  p95 {p:.1%}  {dict(zip(nom, np.round(w, 3)))}", flush=True)
    elegida = "balas5" if var["balas5"]["tasa"] - var["btc_tend"]["tasa"] >= 0.01 else "btc_tend"
    out["decision_balas"] = dict(variantes=var, elegida=elegida,
                                 regla="se queda balas5 sólo si supera a btc_tend por ≥ 1 punto anual en el nivel 20 % (IS)")
    print("→ variante elegida:", elegida, flush=True)
    nom, d = datos(elegida, pref)
    niveles = {}
    for L in NIVELES:
        w, c, p = P.buscar(d, L, n_dir=a.n_dir)
        w2, c2, p2, f = P.verificar(w, d, L)
        ret, retp = P.cartera(w2, *d[1:4])
        eq = np.cumprod(1 + ret); eqp = np.r_[1.0, eq[:-1]] * (1 + retp)
        dd_hist = float(np.min(eqp / np.maximum.accumulate(eq) - 1))
        niveles[f"{int(L * 100)}"] = dict(pesos=dict(zip(nom, map(float, w2))), tasa_is=float(c2), p95_is=float(p2),
                                          escala_verificacion=float(f), caida_historica_pesimista_is=dd_hist)
        print(f"nivel {L:.0%}: tasa IS {c2:.1%}  p95 {p2:.1%}  caída histórica {dd_hist:.1%}  "
              f"{dict(zip(nom, np.round(w2, 3)))}", flush=True)
    out["niveles"] = niveles
    RES.mkdir(exist_ok=True)
    (RES / f"{pref}seleccion_is.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", action="store_true"); ap.add_argument("--n_dir", type=int, default=1500)
    main(ap.parse_args())
