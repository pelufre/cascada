"""Pruebas de la enmienda E15 (2026-10-04): en momentum alts, una entrada elegida que no llega a abrirse se saltea y su
cupo queda libre para la decisión siguiente, como en la especificación de c40 (antes quedaba reservado para esa moneda y
se reintentaba cada día con el tamaño y el stop del día de la señal). Sin internet ni pytest:
    python -m tests.test_e15
"""
import json
import sys
import traceback

import pandas as pd

from cascada import config as C
from cascada.db import Base
from cascada.estrategias import MomentumC40
from cascada.motor import Motor

from .test_auditoria import FakeBolsa, FakePub

T1 = pd.Timestamp("2026-01-06 00:00")      # decisión diaria
T2 = T1 + pd.Timedelta(days=1)
RITMOS = {"BTC": 0.002, "ETH": 0.003, "X": 0.010, "Y": 0.008}     # suba diaria: X tiene el mejor puntaje


class DatosSinteticos:
    """Velas diarias que suben a ritmo fijo (rango ±1 %): régimen encendido, amplitud 100 %, ATR 2 % en todas."""

    def __init__(self, universo):
        self.u = universo            # t -> lista de símbolos del top 50

    def universo(self, t):
        return self.u(pd.Timestamp(t))

    def v1d(self, base, hasta=None, dias=420):
        if base not in RITMOS:
            return pd.DataFrame(columns=["o", "h", "l", "c"])
        r = RITMOS[base]
        idx = pd.date_range(end=pd.Timestamp(hasta).normalize() - pd.Timedelta(days=1), periods=min(dias, 300), freq="D")
        c = 10.0 * (1 + r) ** (idx - pd.Timestamp("2025-01-01")).days.values
        return pd.DataFrame(dict(o=c / (1 + r), h=c * 1.01, l=c * 0.99, c=c), index=idx)


def _montar(universo):
    datos = DatosSinteticos(universo)
    db = Base(":memory:")
    b = FakeBolsa(1000.0)
    pub = FakePub(b)
    # el contrato mínimo de X (1 moneda ≈ 393 USDT) vale más que el lote (~333 USDT): no se puede abrir
    pub.m.update(X=dict(tam=1.0, minimo=1.0, id="X"), Y=dict(tam=0.1, minimo=1.0, id="Y"))
    cfg = C.Config(pesos={"mom_alts": 1.0}, prioridad=["mom_alts"])
    m = Motor(cfg, db, datos, b, pub)
    m.est = {"mom_alts": MomentumC40(slots=1)}
    return m, b, datos, db


def _ciclo(m, b, datos, t):
    b.fijar_precios({s: float(datos.v1d(s, t).c.iat[-1]) for s in RITMOS})
    return m.ciclo(t, precios=dict(b.ultimo), velas_cerradas={})


def e15a_entrada_que_no_abre_libera_el_cupo():
    """X no llega al contrato mínimo: al día siguiente su cupo queda libre y entra la siguiente candidata (Y)."""
    m, b, datos, db = _montar(lambda t: ["X", "Y"] if t < T2 else ["Y"])
    _ciclo(m, b, datos, T1)
    assert not m.libro() and b.posiciones() == {}, (m.libro(), b.posiciones())
    assert list(db.get("est_mom_alts")["pos"]) == ["X"]                     # eligió X y no pudo abrirla
    _ciclo(m, b, datos, T2)                                                 # X ya no está en el top 50
    st = db.get("est_mom_alts")
    assert [L["simbolo"] for L in m.libro().values()] == ["Y"], m.libro()   # antes: cupo tomado por X y nada abierto
    assert st["saltadas"] == 1 and list(st["pos"]) == ["Y"], st


def e15b_la_misma_moneda_se_vuelve_a_elegir_con_los_datos_del_dia():
    """Si X sigue siendo la mejor, se vuelve a elegir como entrada nueva (id, tamaño y stop del día), no la de ayer."""
    m, b, datos, db = _montar(lambda t: ["X", "Y"])
    _ciclo(m, b, datos, T1)
    id1 = db.get("est_mom_alts")["pos"]["X"]["id"]
    _ciclo(m, b, datos, T2)
    st = db.get("est_mom_alts")
    assert st["pos"]["X"]["id"] != id1 and st["pos"]["X"]["id"].endswith(f"{T2:%Y%m%d}"), (id1, st["pos"])
    assert st["saltadas"] == 1, st


def e15c_lote_cerrado_libera_el_cupo_sin_contar_como_saltada():
    """Un lote que salió por stop libera el cupo como antes y no cuenta como entrada salteada."""
    e = MomentumC40(slots=1)
    datos = DatosSinteticos(lambda t: ["X", "Y"])
    st = {"pos": {"Y": dict(id="mom_Y_20260101", frac=0.3, dist=1.0, vivo=True)}, "_cerrados": {"mom_Y_20260101": "stop"}}
    out = e.paso(T1, datos, st, {}, ["X", "Y"], {"X": {}, "Y": {}})
    assert "saltadas" not in st, st
    assert [L["id"] for L in out] == [f"mom_X_{T1:%Y%m%d}"], out


def e15d_lote_propio_no_registrado_se_adopta():
    """Un lote propio abierto que la estrategia no tiene anotado (llenado tardío) ocupa su cupo y conserva su stop."""
    e = MomentumC40(slots=1)
    datos = DatosSinteticos(lambda t: ["X", "Y"])
    ab = {"mom_Y_20260101": dict(id="mom_Y_20260101", simbolo="Y", lado=1, contratos=5.0, precio_entrada=150.0, stop=141.0,
                                 meta=json.dumps(dict(frac=0.3)))}
    st = {}
    out = e.paso(T1, datos, st, ab, ["X", "Y"], {"X": {}, "Y": {}})
    assert [L["id"] for L in out] == ["mom_Y_20260101"], out              # el cupo está ocupado: no abre X
    p = st["pos"]["Y"]
    assert p["vivo"] and abs(p["dist"] - 9.0) < 1e-9 and abs(p["frac"] - 0.3) < 1e-9, p


PRUEBAS = [e15a_entrada_que_no_abre_libera_el_cupo, e15b_la_misma_moneda_se_vuelve_a_elegir_con_los_datos_del_dia,
           e15c_lote_cerrado_libera_el_cupo_sin_contar_como_saltada, e15d_lote_propio_no_registrado_se_adopta]


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
