"""Protocolo §5.6–5.7: congela pesos, variante de balas y nivel elegido por el usuario (mirando sólo IS).

    python -m validacion.congelar --nivel 20
Después: git add validacion/pesos_congelados.json && git commit (el commit es el sello).
"""
import argparse
import hashlib
import json
from pathlib import Path

RAIZ = Path(__file__).resolve().parent


def main(a):
    sel_p = RAIZ / "resultados" / "seleccion_is.json"
    sel = json.loads(sel_p.read_text())
    ver = {}
    for n in sel["niveles"]:
        f = RAIZ / "resultados" / f"verificacion_{n}.json"
        if not f.exists():
            raise SystemExit(f"Falta la verificación con el motor completo del nivel {n} (§5.4): {f.name}")
        ver[n] = json.loads(f.read_text())
    niveles = {n: ver[n]["pesos"] for n in sel["niveles"]}
    if a.nivel not in niveles:
        raise SystemExit(f"Nivel {a.nivel} no está en la selección: {list(niveles)}")
    out = dict(nivel_elegido=a.nivel, variante_balas=sel["decision_balas"]["elegida"], pesos_por_nivel=niveles,
               seleccion_sha256=hashlib.sha256(sel_p.read_bytes()).hexdigest(),
               verificacion_motor={n: dict(factor=v["factor"], cumple=v["cumple"], busqueda=v["busqueda"], resultados=v["resultados"]) for n, v in ver.items()})
    (RAIZ / "pesos_congelados.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--nivel", required=True)
    main(ap.parse_args())
