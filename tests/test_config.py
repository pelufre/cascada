"""Los pesos del servicio son exactamente los congelados por la validación (validacion/pesos_congelados.json).

    python -m tests.test_config
"""
import json
from pathlib import Path

from cascada import config as C

CONG = Path(__file__).resolve().parent.parent / "validacion" / "pesos_congelados.json"


def pesos_iguales_a_los_congelados():
    d = json.loads(CONG.read_text())
    for n, w in d["pesos_por_nivel"].items():
        k = f"nivel_{n}"
        assert set(C.NIVELES[k]) == set(w), k
        for e, v in w.items():
            assert abs(C.NIVELES[k][e] - v) < 1e-9, (k, e, C.NIVELES[k][e], v)
        p95 = d["verificacion_motor"][n]["resultados"]["B"]["p95"]
        assert abs(C.P95_IS[k] - p95) < 5e-4, (k, C.P95_IS[k], p95)


def config_del_repo_es_la_validada():
    c = C.cargar()
    assert c.nivel == "nivel_30" and not c.desactivadas and c.tope_con_balas_real
    assert c.pesos == C.NIVELES["nivel_30"]
    assert (c.alerta_caida, c.corte_caida) == (0.20, 0.30)


def nombre_viejo_se_traduce():
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("nivel: techo_intrabarra_20\n")
    c = C.cargar(f.name)
    assert c.nivel == "nivel_20" and c.pesos == C.NIVELES["nivel_20"]
    assert (c.alerta_caida, c.corte_caida) == (0.13, 0.20)


def main():
    pruebas = [pesos_iguales_a_los_congelados, config_del_repo_es_la_validada, nombre_viejo_se_traduce]
    mal = 0
    for p in pruebas:
        try:
            p(); print("✔", p.__name__)
        except Exception as e:
            mal += 1; print("✘", p.__name__, repr(e))
    print(f"\n{len(pruebas) - mal}/{len(pruebas)} en verde")
    raise SystemExit(1 if mal else 0)


if __name__ == "__main__":
    main()
