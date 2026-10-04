"""Enmienda E15: comparación caso por caso de la revalidación E14 (archivada en resultados/revalidacion/_e14/) con la
del motor 1.3.1 (momentum alts saltea la entrada que no se abre). Diagnóstico: no elige ni cambia nada.

    python -m validacion.comparar_e15
"""
import json
from pathlib import Path

import pandas as pd

REV = Path(__file__).resolve().parent / "resultados" / "revalidacion"
CAMPOS = ("cagr", "dd_pesimista", "dd_estricta", "calmar", "p95")


def _lotes_mom(p):
    if not p.exists():
        return None
    d = pd.read_pickle(p)
    return int((d.estrategia == "mom_alts").sum()) if len(d) else 0


def main():
    out = {}
    for p in sorted((REV / "_e14").glob("*.json")):
        nuevo = REV / p.name
        if p.name == "revalidacion.json" or not nuevo.exists():
            continue
        a, b = json.loads(p.read_text()), json.loads(nuevo.read_text())
        mom = lambda x: (x.get("atribucion") or {}).get("por_estrategia", {}).get("mom_alts")
        out[p.stem] = dict(e14={k: a.get(k) for k in CAMPOS}, e15={k: b.get(k) for k in CAMPOS},
                           mom_saltadas=b.get("mom_saltadas"), resultado_mom_e14=mom(a), resultado_mom_e15=mom(b),
                           lotes_mom_e14=_lotes_mom(REV / "_e14" / f"{p.stem}_lotes.pkl"),
                           lotes_mom_e15=_lotes_mom(REV / f"{p.stem}_lotes.pkl"))
    (REV / "comparacion_e14_e15.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=float))
    f = lambda x: "—" if x is None else f"{x:7.2%}"
    print(f"{'caso':24s} {'tasa E14':>8s} {'tasa E15':>8s} {'caída E14':>9s} {'caída E15':>9s} {'p95 E14':>8s} {'p95 E15':>8s}"
          f" {'saltadas':>8s} {'mom E14':>9s} {'mom E15':>9s}")
    for k, v in out.items():
        a, b = v["e14"], v["e15"]
        m14, m15 = v["resultado_mom_e14"], v["resultado_mom_e15"]
        print(f"{k:24s} {f(a['cagr'])} {f(b['cagr'])} {f(a['dd_pesimista']):>9s} {f(b['dd_pesimista']):>9s} {f(a['p95'])} "
              f"{f(b['p95'])} {v['mom_saltadas']!s:>8s} {m14 if m14 is None else round(m14, 1)!s:>9s} "
              f"{m15 if m15 is None else round(m15, 1)!s:>9s}")
    return out


if __name__ == "__main__":
    main()
