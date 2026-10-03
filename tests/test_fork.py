"""Rama fork-bloques: reparto sin cascada y grupo de estrategias que no pueden estar en sentidos opuestos.

    python -m tests.test_fork
"""
import sys
import traceback

from cascada import config as C
from cascada.db import Base
from cascada.motor import Motor

from .test_auditoria import H4, T0, FakeBolsa, FakeDatos, FakeEst, FakePub, lote


class Est(FakeEst):
    def __init__(self, nombre):
        super().__init__(); self.nombre = nombre


def montar(modo="bloques", pesos=None):
    db = Base(":memory:")
    b = FakeBolsa(1000.0); b.fijar_precios({"BTC": 10.0, "ETH": 10.0})
    cfg = C.Config(pesos=pesos or {"cortos": 0.5, "largos": 0.5, "otra": 0.8}, prioridad=["cortos", "largos", "otra"],
                   modo_capital=modo, tope_nocional=3.0 if modo == "bloques" else 1.0,
                   grupos={"par": ["cortos", "largos"]} if modo == "bloques" else {})
    m = Motor(cfg, db, FakeDatos(), b, FakePub(b))
    e = {k: Est(k) for k in ("cortos", "largos", "otra")}
    m.est = e
    return m, b, e


def ciclo(m, t):
    return m.ciclo(t, precios=dict(m.bolsa.ultimo), velas_cerradas={})


def f01_par_no_abre_el_otro_sentido():
    """Con un corto del par abierto, el largo del par no abre; cuando el corto sale, el largo entra."""
    m, b, e = montar()
    e["cortos"].deseos = [lote("c1", frac=0.5, lado=-1, reesc=False, sim="ETH")]
    ciclo(m, T0)
    e["largos"].deseos = [lote("l1", frac=0.5, lado=1, reesc=False, sim="BTC")]
    ciclo(m, T0 + H4)
    assert "BTC" not in b.posiciones() and b.posiciones().get("ETH", 0) < 0, b.posiciones()
    e["cortos"].deseos = []                                 # el corto sale por su propia regla
    ciclo(m, T0 + 2 * H4)
    assert "ETH" not in b.posiciones(), b.posiciones()
    ciclo(m, T0 + 3 * H4)
    assert b.posiciones().get("BTC", 0) > 0, b.posiciones()


def f02_entrada_simultanea_gana_la_prioridad():
    m, b, e = montar()
    e["cortos"].deseos = [lote("c1", frac=0.5, lado=-1, reesc=False, sim="ETH")]
    e["largos"].deseos = [lote("l1", frac=0.5, lado=1, reesc=False, sim="BTC")]
    ciclo(m, T0)
    assert b.posiciones().get("ETH", 0) < 0 and "BTC" not in b.posiciones(), b.posiciones()


def f03_bloques_sin_cascada():
    """Sin cascada cada estrategia recibe su peso entero aunque la suma pase de 1 (rige sólo el tope de 3×)."""
    m, b, e = montar()
    e["largos"].deseos = [lote("l1", frac=1.0, lado=1, sim="BTC")]
    e["otra"].deseos = [lote("o1", frac=1.0, lado=1, sim="ETH")]
    ciclo(m, T0)
    noc = sum(abs(c) * 10.0 for c in b.posiciones().values())
    assert abs(noc - 1300.0) <= 10.0, (noc, b.posiciones())      # 0,5 × 1000 + 0,8 × 1000
    m2, b2, e2 = montar(modo="cascada")
    e2["largos"].deseos = [lote("l1", frac=1.0, lado=1, sim="BTC")]
    e2["otra"].deseos = [lote("o1", frac=1.0, lado=1, sim="ETH")]
    ciclo(m2, T0)
    noc2 = sum(abs(c) * 10.0 for c in b2.posiciones().values())
    assert noc2 <= 1050.0 + 1e-9, (noc2, b2.posiciones())          # la cascada sigue topeando en 1×


PRUEBAS = [f01_par_no_abre_el_otro_sentido, f02_entrada_simultanea_gana_la_prioridad, f03_bloques_sin_cascada]


def main():
    import logging
    logging.disable(logging.CRITICAL)
    mal = 0
    for p in PRUEBAS:
        try:
            p(); print("✔", p.__name__)
        except Exception as ex:
            mal += 1
            print("✘", p.__name__, "—", (p.__doc__ or "").strip().split("\n")[0])
            print("   ", repr(ex) if isinstance(ex, AssertionError) else traceback.format_exc().strip().splitlines()[-1])
    print(f"\n{len(PRUEBAS) - mal}/{len(PRUEBAS)} en verde")
    sys.exit(1 if mal else 0)


if __name__ == "__main__":
    main()
