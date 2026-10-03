"""Reequilibrio real del capital de 30 balas entre la cuenta principal y su subcuenta (cascada/subcuenta.py).

    python -m tests.test_subcuenta
"""
import sys
import traceback

from cascada import config as C
from cascada.subcuenta import TransferidorSubcuenta, planificar


class Libro:
    """Saldos USDT de las cuatro cuentas en juego."""
    def __init__(self, contract=1000.0, m_main=0.0, s_main=0.0, s_trade=300.0):
        self.s = {("m", "contract"): contract, ("m", "main"): m_main, ("s", "main"): s_main, ("s", "trade"): s_trade}

    def mover(self, a, b, monto):
        if self.s[a] < monto - 1e-9:
            raise RuntimeError(f"saldo insuficiente en {a}")
        self.s[a] -= monto; self.s[b] += monto


class SpotPrincipal:
    def __init__(self, libro, rechazar=()):
        self.libro = libro; self.rechazar = set(rechazar); self.hechas = []

    def transfer(self, code, monto, desde, hacia, p):
        tipo = p["transferType"]
        if (tipo, desde, hacia) in self.rechazar:
            raise RuntimeError(f"{tipo} {desde}->{hacia} no soportado")
        assert p.get("clientOid")
        lado = {"INTERNAL": ("m", "m"), "PARENT_TO_SUB": ("m", "s"), "SUB_TO_PARENT": ("s", "m")}[tipo]
        if tipo == "PARENT_TO_SUB":
            assert p["toUserId"] == "123"
        if tipo == "SUB_TO_PARENT":
            assert p["fromUserId"] == "123"
        self.libro.mover((lado[0], desde), (lado[1], hacia), monto)
        self.hechas.append((tipo, desde, hacia, monto))
        return {"id": "x"}

    def fetch_balance(self, p):
        u = self.libro.s[("m", p["type"])]
        return {"total": {"USDT": u}, "free": {"USDT": u}}


class SpotSub:
    def __init__(self, libro):
        self.libro = libro

    def fetch_balance(self, p):
        return {"free": {"USDT": self.libro.s[("s", p["type"])]}}

    def transfer(self, code, monto, desde, hacia):
        self.libro.mover(("s", desde), ("s", hacia), monto)


class EjecutorSub:
    def __init__(self, libro, btc_fut=0.0):
        self.libro = libro; self.spot = SpotSub(libro); self.btc_fut = btc_fut

    def saldos(self):
        return dict(spot_main={"USDT": self.libro.s[("s", "main")], "BTC": 0.0},
                    spot_trade={"USDT": self.libro.s[("s", "trade")], "BTC": 0.0},
                    futuros_BTC=dict(total=self.btc_fut, libre=self.btc_fut))

    def usdt_a_trading(self):
        u = self.libro.s[("s", "main")]
        if u > 0.01:
            self.libro.mover(("s", "main"), ("s", "trade"), u)
        return u


def transferidor(libro, rechazar=()):
    sp = SpotPrincipal(libro, rechazar)
    return TransferidorSubcuenta("123", EjecutorSub(libro), spot_principal=sp), sp


# ------------------------------------------------------------------ plan
def s01_plan_envia_lo_que_falta():
    p = planificar(E_main=1950.0, libre_main=1500.0, sub_usdt=1050.0, sub_btc_usd=0.0, peso=0.4)
    assert p["accion"] == "enviar" and abs(p["monto"] - 150.0) < 0.01, p          # 0,4 × 3000 = 1200


def s02_plan_trae_el_sobrante():
    p = planificar(E_main=1800.0, libre_main=1000.0, sub_usdt=1400.0, sub_btc_usd=0.0, peso=0.35)
    assert p["accion"] == "traer" and abs(p["monto"] - (1400 - 0.35 * 3200)) < 0.01, p


def s03_plan_diferencia_chica_no_hace_nada():
    p = planificar(E_main=1950.0, libre_main=1500.0, sub_usdt=1045.0, sub_btc_usd=0.0, peso=0.35)
    assert p["accion"] == "nada", p


def s04_plan_respeta_el_colchon_de_la_principal():
    p = planificar(E_main=2000.0, libre_main=260.0, sub_usdt=600.0, sub_btc_usd=0.0, peso=0.35)
    assert p["accion"] == "enviar" and abs(p["monto"] - (260 - 200)) < 0.01 and p["motivo"].startswith("parcial"), p
    p = planificar(E_main=2000.0, libre_main=205.0, sub_usdt=600.0, sub_btc_usd=0.0, peso=0.35)
    assert p["accion"] == "nada" and "margen libre" in p["motivo"], p


def s05_plan_no_trae_btc():
    """Si lo que sobra en la subcuenta es BTC (no debería con la subcuenta plana), sólo devuelve el USDT que hay."""
    p = planificar(E_main=1000.0, libre_main=800.0, sub_usdt=5.0, sub_btc_usd=900.0, peso=0.35)
    assert p["accion"] == "nada", p


# ------------------------------------------------------------------ transferencias
def s06_envia_y_deja_el_usdt_en_trade():
    libro = Libro(contract=1000.0, s_trade=300.0)
    t, sp = transferidor(libro)
    t.enviar(150.0)
    assert libro.s[("m", "contract")] == 850.0 and libro.s[("s", "trade")] == 450.0 and libro.s[("s", "main")] == 0.0, libro.s
    assert sp.hechas == [("PARENT_TO_SUB", "contract", "main", 150.0)], sp.hechas


def s07_ruta_alternativa_y_memoria():
    """Si KuCoin no acepta futuros → subcuenta directo, pasa por la «main» de la principal y recuerda la ruta."""
    libro = Libro(contract=1000.0)
    t, sp = transferidor(libro, rechazar={("PARENT_TO_SUB", "contract", "main")})
    t.enviar(100.0)
    assert libro.s[("s", "trade")] == 400.0 and libro.s[("m", "main")] == 0.0, libro.s
    assert t.ruta_ida.startswith("futuros → main principal"), t.ruta_ida
    n = len(sp.hechas); t.enviar(50.0)
    assert sp.hechas[n:] == [("INTERNAL", "contract", "main", 50.0), ("PARENT_TO_SUB", "main", "main", 50.0)], sp.hechas[n:]


def s08_segundo_paso_falla_y_se_deshace():
    libro = Libro(contract=1000.0)
    t, sp = transferidor(libro, rechazar={("PARENT_TO_SUB", "contract", "main"), ("PARENT_TO_SUB", "main", "main")})
    try:
        t.enviar(100.0)
        raise AssertionError("debió fallar")
    except RuntimeError as e:
        assert "No pude transferir" in str(e), e
    assert libro.s[("m", "contract")] == 1000.0 and libro.s[("m", "main")] == 0.0, libro.s


def s09_trae_desde_trade():
    libro = Libro(contract=500.0, s_trade=800.0)
    t, sp = transferidor(libro)
    t.traer(200.0)
    assert libro.s[("m", "contract")] == 700.0 and libro.s[("s", "trade")] == 600.0, libro.s
    assert sp.hechas == [("SUB_TO_PARENT", "main", "contract", 200.0)], sp.hechas


# ------------------------------------------------------------------ servicio
class BolsaReal:
    modo = "real"

    def __init__(self, libro):
        self.libro = libro

    def patrimonio(self):
        return self.libro.s[("m", "contract")] + 400.0          # + resultado abierto de las posiciones

    def usdt_libre(self):
        return self.libro.s[("m", "contract")] * 0.8


def _sistema_real(libro):
    from .test_servicio import _sistema
    s, pub = _sistema()
    s.bolsa = BolsaReal(libro)
    s.transferidor, _ = transferidor(libro)
    avisos = []
    s.avisar = lambda n, m: avisos.append(m)
    return s, avisos


def s10_servicio_iguala_entre_campañas():
    libro = Libro(contract=1600.0, s_trade=500.0)
    s, avisos = _sistema_real(libro)
    s.capital_balas(50000.0)
    total = 1600.0 + 400.0 + 500.0
    objetivo = C.NIVELES["nivel_30"]["balas5"] * total
    assert abs(libro.s[("s", "trade")] - objetivo) < 0.02, (libro.s, objetivo)
    assert abs(s.balas.st["W"] - libro.s[("s", "trade")]) < 1e-9, s.balas.st["W"]
    assert s.db.get("transferencias_balas") and avisos, "falta el registro o el aviso"


def s11_servicio_no_toca_durante_campaña():
    libro = Libro(contract=1600.0, s_trade=500.0)
    s, avisos = _sistema_real(libro)
    s.balas.st["activo"] = True
    s.capital_balas(50000.0)
    assert libro.s[("s", "trade")] == 500.0 and not avisos


def s12_servicio_falla_sin_romper():
    libro = Libro(contract=1600.0, s_trade=500.0)
    s, avisos = _sistema_real(libro)
    s.transferidor.m.rechazar = {("PARENT_TO_SUB", "contract", "main"), ("PARENT_TO_SUB", "main", "main")}
    s.capital_balas(50000.0)
    assert s.db.filas("SELECT 1 FROM incidencias WHERE tipo='transferencia_balas' AND resuelta=0"), "falta la incidencia"
    assert libro.s[("m", "contract")] == 1600.0 and avisos


PRUEBAS = [s01_plan_envia_lo_que_falta, s02_plan_trae_el_sobrante, s03_plan_diferencia_chica_no_hace_nada,
           s04_plan_respeta_el_colchon_de_la_principal, s05_plan_no_trae_btc, s06_envia_y_deja_el_usdt_en_trade,
           s07_ruta_alternativa_y_memoria, s08_segundo_paso_falla_y_se_deshace, s09_trae_desde_trade,
           s10_servicio_iguala_entre_campañas, s11_servicio_no_toca_durante_campaña, s12_servicio_falla_sin_romper]


def main():
    import logging
    import cascada.subcuenta as SC
    import cascada.principal as PR
    logging.disable(logging.CRITICAL)
    SC.time.sleep = PR.time.sleep = lambda *_: None            # sin esperas en las pruebas
    mal = 0
    for p in PRUEBAS:
        try:
            p(); print("✔", p.__name__)
        except Exception as e:
            mal += 1
            print("✘", p.__name__, "—", (p.__doc__ or "").strip().split("\n")[0])
            print("   ", repr(e) if isinstance(e, AssertionError) else traceback.format_exc().strip().splitlines()[-1])
    print(f"\n{len(PRUEBAS) - mal}/{len(PRUEBAS)} en verde")
    sys.exit(1 if mal else 0)


if __name__ == "__main__":
    main()
