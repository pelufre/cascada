"""Pruebas de los hallazgos de la segunda auditoría (V01–V10, 2026-10-03).

Cada prueba arma la situación que reprodujo el auditor (pruebas_independientes.py) o una ventana de falla vecina y
comprueba el comportamiento correcto. Sin internet ni pytest:
    python -m tests.test_auditoria2
"""
import sys
import traceback

import numpy as np
import pandas as pd

from cascada import config as C
from cascada.db import Base
from cascada.motor import Motor

from .test_auditoria import H4, T0, FakeBolsa, FakeDatos, FakeEst, FakePub, ciclo, libro_neto, lote, montar


# ================================================================== V04 · órdenes recuperables en todas las ventanas
def v04a_reinicio_con_estado_terminal_sin_aplicar():
    """Caída del proceso al aplicar una orden ya llenada: nada queda a medias y al recuperar libro = exchange."""
    m, b, e, db = montar()
    e.deseos = [lote(frac=0.1)]
    original = m._aplicar

    def muere(*a, **k):
        raise KeyboardInterrupt("proceso terminado")
    m._aplicar = muere
    try:
        m.ciclo(T0, precios=dict(b.ultimo))
    except KeyboardInterrupt:
        pass
    m._aplicar = original
    o = db.filas("SELECT estado, aplicado, llenado_aplicado FROM ordenes")
    assert o == [dict(estado="enviando", aplicado=0, llenado_aplicado=None)], o      # la transacción no dejó nada
    m._recuperar()
    assert libro_neto(m) == b.posiciones() == {"BTC": 10}, (libro_neto(m), b.posiciones())
    assert db.filas("SELECT estado, aplicado FROM ordenes") == [dict(estado="cerrada", aplicado=1)]


def v04b_caida_entre_dos_escrituras_del_libro():
    """Una orden que reparte entre dos lotes y el proceso muere después de escribir el primero: no queda una parte de la
    asignación escrita; la recuperación aplica todo una sola vez."""
    m, b, e, db = montar()
    e.deseos = [lote("a", frac=0.1), lote("b", frac=0.05)]
    original = m._crear
    n = {"k": 0}

    def segundo_muere(*a, **k):
        n["k"] += 1
        if n["k"] == 2:
            raise KeyboardInterrupt("proceso terminado")
        return original(*a, **k)
    m._crear = segundo_muere
    try:
        m.ciclo(T0, precios=dict(b.ultimo))
    except KeyboardInterrupt:
        pass
    m._crear = original
    assert not m.libro(), f"quedó una parte del reparto escrita: {list(m.libro())}"
    m._recuperar()
    assert libro_neto(m) == b.posiciones() == {"BTC": 15}, (libro_neto(m), b.posiciones())
    assert sorted(m.libro()) == ["a", "b"]


def v04c_orden_abierta_que_llena_tarde():
    """El exchange devuelve la orden abierta con 3 de 10: se aplican 3, la orden queda pendiente (nunca aplicada del
    todo) y cuando llena el resto la recuperación suma los 7."""
    m, b, e, db = montar()
    e.deseos = [lote(frac=0.1)]
    original = b.enviar_orden

    def abierta(*a, **k):
        b.llenar_solo = [3]
        r = original(*a, **k); r["estado"] = "abierta"
        return r
    b.enviar_orden = abierta
    ciclo(m, T0)
    b.enviar_orden = original
    assert libro_neto(m) == {"BTC": 3} and db.filas("SELECT estado, aplicado FROM ordenes") == [dict(estado="abierta", aplicado=0)]
    oid = list(b.hechas)[0]
    b._llenar("BTC", 7, 10); b.hechas[oid].update(estado="cerrada", llenado=10)
    m._recuperar()
    assert libro_neto(m) == b.posiciones() == {"BTC": 10}, (libro_neto(m), b.posiciones())
    assert db.filas("SELECT estado, aplicado FROM ordenes") == [dict(estado="cerrada", aplicado=1)]
    L = m.libro()["a"]
    assert abs(L["precio_entrada"] - 10.0) < 1e-9 and abs(L["contratos"]) == 10


def v04d_pendiente_bloquea_aumentos():
    """Mientras una orden del símbolo no está resuelta, el motor no manda otra que agrande (podría duplicar)."""
    m, b, e, db = montar()
    e.deseos = [lote(frac=0.1)]
    original = b.enviar_orden

    def abierta(*a, **k):
        b.llenar_solo = [3]
        r = original(*a, **k); r["estado"] = "abierta"
        return r
    b.enviar_orden = abierta
    ciclo(m, T0)
    b.enviar_orden = original
    oid = list(b.hechas)[0]
    b.estado_orden = lambda base, coid: dict(b.hechas[coid], estado="abierta") if coid == oid else b.hechas.get(coid)
    n = len(b.ordenes)
    ciclo(m, T0 + H4)
    assert len(b.ordenes) == n, "mandó otra orden con una pendiente en el símbolo"
    assert libro_neto(m) == b.posiciones() == {"BTC": 3}


# ================================================================== V05 · un solo lado por símbolo desde plano
def v05a_opuestos_desde_plano():
    """Largo 10 y corto 5 desde plano: entra sólo el de mayor prioridad (o el primero si empatan); libro = exchange."""
    m, b, e, db = montar()
    e.deseos = [lote("largo", frac=0.1, lado=1), lote("corto", frac=0.05, lado=-1)]
    ciclo(m, T0)
    assert b.posiciones() == {"BTC": 10} and libro_neto(m) == {"BTC": 10}, (b.posiciones(), libro_neto(m))
    assert db.get("conciliacion_ok") is True


def _dos_estrategias():
    db = Base(":memory:")
    b = FakeBolsa(1000.0); b.fijar_precios({"BTC": 10.0, "ETH": 10.0})
    cfg = C.Config(pesos={"x": 0.5, "y": 0.5}, prioridad=["x", "y"])
    m = Motor(cfg, db, FakeDatos(), b, FakePub(b))
    ex, ey = FakeEst(), FakeEst()
    m.est = {"x": ex, "y": ey}
    return m, b, ex, ey, db


def v05b_prioridad_decide_el_lado():
    """Dos estrategias piden lados opuestos sobre BTC plano: entra la de mayor prioridad aunque pida menos."""
    m, b, ex, ey, db = _dos_estrategias()
    ey.deseos = [lote("y1", frac=0.2, lado=1)]
    ex.deseos = [lote("x1", frac=0.1, lado=-1)]
    ciclo(m, T0)
    assert b.posiciones() == {"BTC": -5} and libro_neto(m) == {"BTC": -5}, (b.posiciones(), libro_neto(m))


def v05c_cambio_de_lado_en_el_mismo_ciclo():
    """Un largo que se cierra y un corto nuevo de mayor prioridad en el mismo ciclo: primero se cierra, después se abre
    el corto, en dos órdenes; libro = exchange."""
    m, b, ex, ey, db = _dos_estrategias()
    ey.deseos = [lote("y1", frac=0.2, lado=1)]
    ciclo(m, T0)
    assert b.posiciones() == {"BTC": 10}
    ey.deseos = []
    ex.deseos = [lote("x1", frac=0.1, lado=-1)]
    ciclo(m, T0 + H4)
    assert b.posiciones() == {"BTC": -5} and libro_neto(m) == {"BTC": -5}, (b.posiciones(), libro_neto(m))
    assert db.get("conciliacion_ok") is True


# ================================================================== V06 · transferencias sin duplicar
class _PrincipalQuePierdeRespuestas:
    """Principal cuyo primer PARENT_TO_SUB pierde la respuesta: `hacer=True` lo acredita antes de fallar, False no."""
    def __init__(self, libro, hacer=True):
        from .test_subcuenta import SpotPrincipal
        self.base = SpotPrincipal(libro); self.hacer = hacer; self.fallas = 1; self.oids = []

    def transfer(self, code, monto, desde, hacia, p):
        self.oids.append(p["clientOid"])
        if p["transferType"] == "PARENT_TO_SUB" and self.fallas:
            self.fallas -= 1
            if self.hacer:
                self.base.transfer(code, monto, desde, hacia, p)
            raise TimeoutError("respuesta perdida")
        return self.base.transfer(code, monto, desde, hacia, p)

    def fetch_balance(self, p):
        return self.base.fetch_balance(p)


def _transferidor_con(libro, principal, db=None):
    from cascada.subcuenta import TransferidorSubcuenta
    from .test_subcuenta import EjecutorSub
    return TransferidorSubcuenta("123", EjecutorSub(libro), spot_principal=principal, db=db)


def v06a_respuesta_perdida_no_duplica():
    """El exchange acredita 100 y se pierde la respuesta: se confirma por saldo y NO se prueba la otra ruta."""
    from .test_subcuenta import Libro
    libro = Libro(contract=1000.0, s_trade=300.0)
    pr = _PrincipalQuePierdeRespuestas(libro, hacer=True)
    db = Base(":memory:")
    t = _transferidor_con(libro, pr, db)
    t.enviar(100.0)
    assert libro.s[("m", "contract")] == 900.0 and libro.s[("s", "trade")] == 400.0, libro.s
    assert pr.base.hechas == [("PARENT_TO_SUB", "contract", "main", 100.0)], pr.base.hechas
    r = db.filas("SELECT estado, monto FROM transferencias")
    assert r == [dict(estado="hecha", monto=100.0)], r


def v06b_sin_respuesta_y_sin_confirmar_queda_incierta():
    """Se pierde la respuesta y el saldo no muestra el movimiento: queda incierta, no se prueba otra ruta; en el ciclo
    siguiente los saldos dicen que no se hizo y se libera."""
    from cascada.subcuenta import TransferenciaIncierta
    from .test_subcuenta import Libro
    libro = Libro(contract=1000.0, s_trade=300.0)
    pr = _PrincipalQuePierdeRespuestas(libro, hacer=False)
    db = Base(":memory:")
    t = _transferidor_con(libro, pr, db)
    try:
        t.enviar(100.0)
        raise AssertionError("debió quedar incierta")
    except TransferenciaIncierta:
        pass
    assert libro.s[("m", "contract")] == 1000.0 and not pr.base.hechas, (libro.s, pr.base.hechas)
    assert len(pr.oids) == 1, f"probó otra ruta: {pr.oids}"
    assert [p["estado"] for p in t.pendientes()] == ["incierta"]
    assert t.resolver() == [] and db.filas("SELECT estado FROM transferencias") == [dict(estado="fallida")]


def v06c_servicio_bloquea_balas_mientras_hay_incierta():
    """El servicio no transfiere ni deja abrir campaña mientras una transferencia esté incierta."""
    from .test_subcuenta import Libro, _sistema_real
    libro = Libro(contract=1600.0, s_trade=500.0)
    s, avisos = _sistema_real(libro)
    pr = _PrincipalQuePierdeRespuestas(libro, hacer=False)
    s.transferidor = _transferidor_con(libro, pr, s.db)
    s.capital_balas(50000.0)
    assert s.db.get("balas_bloqueo_transferencia") is True and libro.s[("m", "contract")] == 1600.0
    s.transferidor.m.fallas = 0
    s.capital_balas(50000.0)                       # el saldo muestra que no se hizo: se libera y se iguala
    assert s.db.get("balas_bloqueo_transferencia") is False, s.db.get("balas_bloqueo_transferencia")
    objetivo = C.NIVELES["nivel_30"]["balas5"] * (1600.0 + 400.0 + 500.0)
    assert abs(libro.s[("s", "trade")] - objetivo) < 0.02, (libro.s, objetivo)


# ================================================================== 30 balas: subcuenta falsa para el modo real
class SubcuentaFalsa:
    """Ejecutor real falso con saldos de verdad: USDT, BTC en futuros y en spot, contratos XBTUSDM (1 USD) y precio.
    fallar: {acción: "antes" | "medio" | "despues"}: falla con TimeoutError antes de hacer nada, a mitad (margen comprado,
    sin contratos) o después de hacerlo todo (respuesta perdida)."""
    def __init__(self, usdt, px, fallar=None):
        self.usdt = usdt; self.btc_fut = 0.0; self.btc_spot = 0.0; self.N = 0.0; self.E = None; self.px = px
        self.fallar = dict(fallar or {}); self.llamadas = []

    def estado(self):
        return dict(usdt=self.usdt, btc_spot=self.btc_spot, margen_btc=self.btc_fut, contratos=self.N, entrada=self.E)

    def patrimonio(self):
        return self.usdt + (self.btc_fut + self.btc_spot) * self.px + (self.N * (self.px / self.E - 1) if self.N else 0.0)

    def _margen(self, usd):
        btc = usd / self.px
        self.usdt -= usd; self.btc_fut += btc

    def _contratos(self, n):
        n = int(n)
        if n >= 1:
            self.E = self.px if not self.N else (self.N + n) / (self.N / self.E + n / self.px)
            self.N += n

    def _accion(self, nombre, margen, n):
        self.llamadas.append(nombre)
        modo = self.fallar.pop(nombre, None)
        if modo == "antes":
            raise TimeoutError("sin respuesta")
        self._margen(margen)
        if modo == "medio":
            raise TimeoutError("cayó a mitad")
        self._contratos(n)
        if modo == "despues":
            raise TimeoutError("respuesta perdida")
        return dict(ok=True)

    def abrir(self, usd_margen, nocional_usd, px=None):
        return self._accion("abrir", usd_margen, nocional_usd)

    def recargar(self, usd_margen, nocional_usd, px=None):
        return self._accion("recargar", usd_margen, nocional_usd)

    def aportar_margen(self, usd, px=None):
        return self._accion("aportar_margen", usd, 0)

    def a_futuros_todo(self):
        self.llamadas.append("a_futuros_todo"); self.btc_fut += self.btc_spot; self.btc_spot = 0.0

    def cerrar(self, px=None):
        self.llamadas.append("cerrar")
        if self.fallar.pop("cerrar", None) == "antes":
            raise TimeoutError("sin respuesta")
        if self.N:
            self.btc_fut += self.N / self.E - self.N / self.px; self.N = 0.0; self.E = None
        self.usdt += (self.btc_fut + self.btc_spot) * self.px; self.btc_fut = self.btc_spot = 0.0
        return {}


def _balas_real(sub, W=600.0):
    from cascada.balas import Balas
    db = Base(":memory:")
    bl = Balas(db, W, real=sub)
    bl.st.update(calentado=True, regWeekly=True, momT=True, levm=1.0, dC=[60.0] * 250, emaHist=[50.0] * 6, emaW=50.0)
    return bl, db


def _cierres_con_senal(t, px):
    """Cierres que bajan fuerte al final: RSI(2) muy bajo (señal de entrada) con el último cierre en px."""
    c = np.r_[np.full(97, px * 1.10), px * 1.06, px * 1.03, px]
    return pd.Series(c, index=pd.date_range(end=t - H4, periods=100, freq="4h"))


def v03a_apertura_real_que_falla_no_inventa_estado():
    """La apertura real falla sin respuesta antes de hacer nada: el estado NO queda activo con un nocional inventado;
    30 balas se bloquea, lo anota y, leída la subcuenta, sigue."""
    sub = SubcuentaFalsa(600.0, 100.0, fallar={"abrir": "antes"})
    bl, db = _balas_real(sub)
    t = pd.Timestamp("2026-01-07 04:00")
    bl.decidir(t, _cierres_con_senal(t, 100.0), px=100.0)
    assert sub.llamadas == ["abrir"], sub.llamadas
    assert not bl.st["activo"] and bl.st["ntn"] == 0.0 and bl.st["bloqueo"], bl.st
    assert db.filas("SELECT tipo, estado FROM balas_acciones") == [dict(tipo="abrir", estado="incierta")]
    bl.decidir(t + H4, _cierres_con_senal(t + H4, 100.0), px=100.0)        # la subcuenta está limpia: se desbloquea
    assert not bl.st["bloqueo"], bl.st["bloqueo"]
    assert abs(bl.patrimonio(100.0) - sub.patrimonio()) < 1e-6


def v03b_apertura_a_medias_se_deshace():
    """Compró el margen y cayó antes de los contratos: queda bloqueada; al ciclo siguiente vende el BTC suelto, queda
    coherente con la subcuenta y recién entonces vuelve a decidir."""
    sub = SubcuentaFalsa(600.0, 100.0, fallar={"abrir": "medio"})
    bl, db = _balas_real(sub)
    t = pd.Timestamp("2026-01-07 04:00")
    bl.decidir(t, _cierres_con_senal(t, 100.0), px=100.0)
    assert bl.st["bloqueo"] and not bl.st["activo"] and sub.btc_fut > 0, (bl.st["bloqueo"], sub.btc_fut)
    assert abs(bl.patrimonio(100.0) - sub.patrimonio()) < 1e-6, (bl.patrimonio(100.0), sub.patrimonio())
    bl.decidir(t + H4, _cierres_con_senal(t + H4, 100.0), px=100.0)
    assert "cerrar" in sub.llamadas and not bl.st["bloqueo"], (sub.llamadas, bl.st["bloqueo"])
    assert abs(bl.patrimonio(100.0) - sub.patrimonio()) < 1e-6


def v03c_respuesta_perdida_tras_abrir_toma_lo_real():
    """La apertura se hizo pero se perdió la respuesta: el estado toma la campaña que de verdad está abierta, con las
    cantidades de la subcuenta (contratos enteros), y no la vuelve a abrir."""
    sub = SubcuentaFalsa(600.0, 100.0, fallar={"abrir": "despues"})
    bl, db = _balas_real(sub)
    t = pd.Timestamp("2026-01-07 04:00")
    bl.decidir(t, _cierres_con_senal(t, 100.0), px=100.0)
    st = bl.st
    assert st["activo"] and st["camps"] == 1 and sub.N == st["ntn"] * st["W"] and sub.N == int(sub.N), (st["ntn"] * st["W"], sub.N)
    assert abs(st["inv"] * st["W"] - sub.N / sub.E) < 1e-9 and abs(bl.patrimonio(100.0) - sub.patrimonio()) < 1e-6
    bl.decidir(t + H4, _cierres_con_senal(t + H4, 100.0), px=100.0)
    assert sub.llamadas.count("abrir") == 1, sub.llamadas


def v03d_patrimonio_y_nocional_son_los_reales():
    """Durante la campaña, lo que ve el motor (patrimonio y nocional de balas) es lo de la subcuenta, no un estado sombra."""
    sub = SubcuentaFalsa(600.0, 100.0)
    bl, db = _balas_real(sub)
    t = pd.Timestamp("2026-01-07 04:00")
    bl.decidir(t, _cierres_con_senal(t, 100.0), px=100.0)
    sub.px = 90.0; sub.btc_fut -= 0.0001                     # baja el precio y el exchange cobra funding
    bl.sincronizar(90.0)
    assert abs(bl.patrimonio(90.0) - sub.patrimonio()) < 1e-6 and abs(bl.nocional() - sub.N) < 1e-9


def o15b_recarga_solo_margen_real():
    """Una recarga que sólo agrega margen (sin contratos) también se ejecuta en la subcuenta real y se cuenta."""
    sub = SubcuentaFalsa(600.0, 100.0)
    bl, db = _balas_real(sub)
    t = pd.Timestamp("2026-01-07 04:00")
    bl.decidir(t, _cierres_con_senal(t, 100.0), px=100.0)
    usdt = sub.usdt; usados = bl.st["used"]
    sub.px = 62.0                                            # ROE muy negativo: recarga sin contratos (ratio 0)
    t2 = pd.Timestamp("2026-01-08 00:00")
    cs = pd.Series(np.linspace(70, 62, 100), index=pd.date_range(end=t2 - H4, periods=100, freq="4h"))
    bl.decidir(t2, cs, px=62.0)
    assert "recargar" in sub.llamadas and sub.usdt < usdt and bl.st["used"] > usados, (sub.llamadas, sub.usdt, bl.st["used"])


# ================================================================== V02 · reserva antes de la vela, nunca después
def _balas_papel_activa(px_ref):
    from cascada.balas import Balas
    bl = Balas(Base(":memory:"), 1000.0)
    bl.st.update(activo=True, ntn=1.0, inv=1 / 100, mbtc=0.0007, contrib=0.07, resd=False, calentado=True, used=1,
                 ultimo_precio=px_ref, entry_t="2026-01-01 00:00", rec=True)
    return bl


def v02a_la_reserva_no_protege_una_vela_pasada():
    """Liquidación sin reserva en 94,11; la decisión anterior fue con precio lejos (> 12 %), así que no hay reserva puesta;
    la vela abre en 95 y toca 90: se liquida. Nada se aporta 'al cierre' para salvar la vela que ya pasó."""
    bl = _balas_papel_activa(110.0)
    liq = bl._liq()
    assert 90.0 < liq < 95.0 and 110.0 / liq - 1 > 0.12, liq          # 94,4 con el tramo de 9 BTC (1 %)
    bl.vela(pd.Timestamp("2026-01-07 04:00"), (95.0, 100.0, 90.0, 100.0))
    assert bl.st["liqs"] == 1 and not bl.st["activo"], bl.st


def v02b_reserva_decidida_antes_protege_la_vela_siguiente():
    """Si al decidir el precio ya está a menos de 12 % de la liquidación, la reserva se pone en ese momento y protege la
    vela siguiente."""
    bl = _balas_papel_activa(101.0)
    t = pd.Timestamp("2026-01-07 00:00")
    cs = pd.Series(np.full(100, 101.0), index=pd.date_range(end=t - H4, periods=100, freq="4h"))
    bl.st.update(regWeekly=True, used=30)                     # sin recarga: sólo la reserva
    bl.decidir(t, cs, px=101.0)
    assert bl.st["resd"], "no puso la reserva al decidir"
    bl.vela(t + H4, (95.0, 100.0, 90.0, 100.0))
    assert bl.st["liqs"] == 0 and bl.st["activo"], bl.st


# ================================================================== V07 · funding del contrato correcto, por evento
def v07a_funding_de_balas_es_el_del_inverso():
    """El servicio pide el funding de 30 balas a XBTUSDM (BTC/USD:BTC), no al perpetuo USDT."""
    from cascada.bolsa import INVERSO
    from .test_servicio import _sistema
    s, pub = _sistema()
    pedidos = []
    pub.funding_liquidado = lambda base, desde, simbolo=None: pedidos.append(simbolo) or []
    s.eventos_funding_balas(pd.Timestamp("2026-10-03 08:00"))
    assert pedidos == [INVERSO] and INVERSO == "BTC/USD:BTC", pedidos


def v07b_funding_por_evento_con_la_posicion_de_ese_momento():
    """Un evento al cierre se cobra completo a la posición que hay; si la vela liquidó, no se cobra."""
    bl = _balas_papel_activa(110.0)
    t = pd.Timestamp("2026-01-07 04:00")
    m0 = bl.st["mbtc"]
    bl.vela(t, (110.0, 111.0, 109.0, 110.0), [(t, 0.001)])
    assert abs((m0 - bl.st["mbtc"]) - 1.0 * 0.001 / 110.0) < 1e-12 and abs(bl.st["funding_usd"] - 1000 * 0.001) < 1e-9
    bl2 = _balas_papel_activa(110.0)
    bl2.vela(t, (95.0, 100.0, 90.0, 100.0), [(t, 0.001)])
    assert bl2.st["liqs"] == 1 and bl2.st["funding_usd"] == 0.0, bl2.st["funding_usd"]


def v07c_funding_despues_de_los_stops_en_papel():
    """Papel: una posición que el stop cerró dentro de la vela no paga el funding del cierre; uno dentro de la vela, si es
    costo, sí (el orden no se conoce: el desfavorable)."""
    from cascada.bolsa import Papel
    db = Base(":memory:")
    p = Papel(db, {"BTC": dict(tam=1.0, minimo=1.0)}, capital=1000, comision=0.0, desliz=0.0)
    p.fijar_precios({"BTC": 100.0})
    p.enviar_orden("BTC", "buy", 10)
    p.enviar_stop("BTC", "sell", 10, 90.0)
    t = pd.Timestamp("2026-01-07 04:00")
    antes = {"BTC": 10.0}
    p.revisar_stops({"BTC": (100.0, 101.0, 85.0)})
    caja = p.st["caja"]
    pagos = p.funding_vela(t, {"BTC": [(t, 0.01)]}, antes, {"BTC": 95.0})
    assert not pagos and p.st["caja"] == caja, (pagos, p.st["caja"], caja)
    pagos = p.funding_vela(t, {"BTC": [(t - pd.Timedelta(hours=2), 0.01)]}, antes, {"BTC": 95.0})
    assert abs(pagos["BTC"] - 10 * 95.0 * 0.01) < 1e-9, pagos


def v07d_stop_con_deslizamiento():
    """El stop del papel es una orden a mercado: llena con deslizamiento (y a la apertura si la vela abrió más allá)."""
    from cascada.bolsa import Papel
    p = Papel(Base(":memory:"), {"BTC": dict(tam=1.0, minimo=1.0)}, capital=1000, comision=0.0, desliz=0.001)
    p.fijar_precios({"BTC": 100.0})
    p.enviar_orden("BTC", "buy", 10)
    oid = p.enviar_stop("BTC", "sell", 10, 90.0)
    h = p.revisar_stops({"BTC": (80.0, 95.0, 70.0)})
    assert abs(h[oid]["precio"] - 80.0 * 0.999) < 1e-9 and h[oid]["gap"], h


# ================================================================== V09 · costos conmutables también en 30 balas
def v09_balas_sin_costos_ni_funding_inventado():
    """Con costos apagados, entrar y salir al mismo precio deja el capital igual; sin eventos no se cobra funding (no
    hay una tasa por defecto)."""
    from cascada.balas import Balas
    bl = Balas(Base(":memory:"), 1000.0, costos=False, contrato_usd=None)
    t = pd.Timestamp("2026-01-07 04:00")
    bl._ejecutar(t, ("entrar", 1.0, False), 100.0)
    bl.vela(t + H4, (100.0, 100.0, 100.0, 100.0), ())
    bl._ejecutar(t + H4, ("salir",), 100.0)
    assert abs(bl.st["W"] - 1000.0) < 1e-9 and bl.st["funding_usd"] == 0.0 and bl.st["comisiones_usd"] == 0.0, bl.st["W"]
    bl2 = Balas(Base(":memory:"), 1000.0)
    bl2._ejecutar(t, ("entrar", 1.0, False), 100.0)
    bl2._ejecutar(t + H4, ("salir",), 100.0)
    assert bl2.st["W"] < 1000.0 and bl2.st["comisiones_usd"] > 0, bl2.st["W"]


class _CcxtFalso:
    """ccxt mínimo para EjecutorRealBalas: órdenes con estados programados, posición, saldos y transferencias."""
    id = "falso"

    def __init__(self):
        self.estados = ["closed"]; self.cancelaciones = 0; self.contratos = 0.0; self.btc_fut = 0.0; self.btc_trade = 0.0
        self.transferencias = []; self.perder_respuesta = 0; self.no_cierra = False

    def load_markets(self, *a):
        return {}

    def create_order(self, sym, tipo, lado, n, precio=None, params=None):
        if lado == "sell" and (params or {}).get("reduceOnly") and not self.no_cierra:
            self.contratos = max(self.contratos - n, 0.0)
        return dict(id="o1")

    def fetch_order(self, oid, sym=None, params=None):
        e = self.estados.pop(0) if len(self.estados) > 1 else self.estados[0]
        return dict(id=oid, status=e, filled=1.0, average=100.0)

    def cancel_order(self, oid, sym=None, params=None):
        self.cancelaciones += 1

    def fetch_positions(self, syms=None):
        return [dict(contracts=self.contratos, side="long", entryPrice=100.0)] if self.contratos else []

    def fetch_balance(self, p=None):
        p = p or {}
        if p.get("code") == "BTC":
            return {"total": {"BTC": self.btc_fut}, "free": {"BTC": self.btc_fut}}
        return {"total": {"BTC": self.btc_trade, "USDT": 0.0}, "free": {"BTC": self.btc_trade, "USDT": 0.0}}

    def transfer(self, code, monto, desde, hacia, params=None):
        self.transferencias.append((desde, hacia, monto))
        self.btc_trade -= monto; self.btc_fut += monto
        if self.perder_respuesta:
            self.perder_respuesta -= 1
            raise TimeoutError("respuesta perdida")
        return dict(id="t")


def _ejecutor(ex):
    from cascada.balas_real import EjecutorRealBalas
    e = EjecutorRealBalas({}, spot=ex, fut=ex)
    e.INTENTOS = 3; e.ESPERA = 0.0
    return e


def v03e_orden_sin_estado_terminal_no_se_da_por_hecha():
    """Una orden que sigue abierta: se cancela el resto y, si igual no hay estado terminal, se lanza SinConfirmar."""
    from cascada.balas_real import SinConfirmar
    ex = _CcxtFalso(); ex.estados = ["open"]
    e = _ejecutor(ex)
    try:
        e.contratos(10)
        raise AssertionError("debió lanzar SinConfirmar")
    except SinConfirmar:
        pass
    assert ex.cancelaciones == 1, ex.cancelaciones
    ex2 = _CcxtFalso(); ex2.estados = ["open", "open", "open", "canceled"]
    assert _ejecutor(ex2).contratos(10)["status"] == "canceled" and ex2.cancelaciones == 1


def v03f_cierre_verificado():
    """cerrar() comprueba que la posición quedó en cero; si no, SinConfirmar (no se informa un cierre que no pasó)."""
    from cascada.balas_real import SinConfirmar
    ex = _CcxtFalso(); ex.contratos = 50.0; ex.no_cierra = True
    e = _ejecutor(ex)
    try:
        e.cerrar()
        raise AssertionError("debió lanzar SinConfirmar")
    except SinConfirmar:
        pass
    ex2 = _CcxtFalso(); ex2.contratos = 50.0
    _ejecutor(ex2).cerrar()
    assert ex2.contratos == 0.0


def v03g_transferencia_interna_con_respuesta_perdida():
    """BTC spot → futuros: se pierde la respuesta pero el saldo de futuros lo muestra: no se manda otra ruta."""
    ex = _CcxtFalso(); ex.btc_trade = 0.01; ex.perder_respuesta = 1
    e = _ejecutor(ex)
    e.a_futuros(0.01)
    assert ex.transferencias == [("trade", "future", 0.01)] and abs(ex.btc_fut - 0.01) < 1e-12, ex.transferencias


# ================================================================== V01 · valoración después de los stops y última vela
class _EstStop:
    """Compra 10 unidades a 100 con stop en 90 (los ejemplos del auditor); `mantener` sigue pidiendo el lote."""
    nombre = "fake"; tf = "4h"

    def decide_en(self, t):
        return True

    def paso(self, t, datos, st, ab, *a):
        if t == T0:
            return [lote("a", frac=1.0, reesc=False, stop_dist=10)]
        return [lote("a", frac=1.0, reesc=False, stop_dist=10)] if ab else []


def _mini_backtest(vela, n_despues=2, est=None):
    """Corrida de 1000 USDT sin costos sobre BTC: dos velas planas en 100, `vela` y n_despues velas planas."""
    from validacion import motor_bt as MB
    filas = [(100.0, 100.0, 100.0, 100.0)] * 2 + [vela] + [(100.0, 100.0, 100.0, 100.0)] * n_despues
    idx = pd.date_range(T0 - H4, periods=len(filas), freq="4h")

    def cargar(db):
        d = pd.DataFrame(filas, index=idx, columns=["o", "h", "l", "c"]); d["v"] = 1.0
        db.guardar_velas("BTC", "4h", d)
        return ["BTC"], {k: d[[k]].rename(columns={k: "BTC"}) for k in d}, None
    orig = MB.todas
    MB.todas = lambda: {"fake": est or _EstStop()}
    try:
        cfg = C.Config(pesos={"fake": 1.0}, prioridad=["fake"], comision=0.0, corte_caida=9.0, alerta_caida=9.0)
        r = MB.Corrida(None, None, None, None, {"fake": 1.0}, modo="PAPEL", corrida="B", capital=1000.0, desde=T0,
                       hasta=idx[-1] + H4, cargar=cargar, mercados={"BTC": dict(tam=1.0, minimo=1.0, id="BTC")},
                       log_cada=0, sin_costos=True, cfg=cfg).correr()
    finally:
        MB.todas = orig
    return r, MB.metricas(r.serie)


def v01a_stop_con_salto_queda_dentro_de_la_medida():
    """Vela que abre en 50 con stop en 90: el stop llena en 50 y la serie lo muestra en esa misma vela (−50 %), no
    un patrimonio de 1000 con 'peor punto' 900."""
    r, m = _mini_backtest((50.0, 100.0, 50.0, 100.0))
    fila = r.serie.loc[T0 + 2 * H4]
    assert fila.E == 500.0 and fila.E_peor == 500.0, fila.to_dict()
    assert r.db.get("papel")["caja"] == 500.0 and r.serie.E.iat[-1] == 500.0
    assert abs(m["dd_optimista"] + 0.5) < 1e-12 and abs(m["dd_estricta"] + 0.5) < 1e-12, m


def v01b_ganancia_posterior_al_stop_no_cuenta():
    """Vela 100 → 120 (máximo y cierre) con mínimo 80 y stop en 90: el patrimonio de esa vela es 900 (lo realizado), no
    1200; la medida estricta toma el máximo 1200 antes del mínimo (−25 %)."""
    r, m = _mini_backtest((100.0, 120.0, 80.0, 120.0))
    fila = r.serie.loc[T0 + 2 * H4]
    assert fila.E == 900.0 and fila.E_peor == 900.0 and fila.E_mejor == 1200.0, fila.to_dict()
    assert r.serie.E.iat[-1] == 900.0 and r.db.get("papel")["caja"] == 900.0
    assert abs(m["dd_optimista"] + 0.1) < 1e-12 and abs(m["dd_estricta"] + 0.25) < 1e-12, m


class _EstMantener(_EstStop):
    def paso(self, t, datos, st, ab, *a):
        return [lote("a", frac=1.0, reesc=False)]


def v01c_ultima_vela_valorada_y_cuentas_cerradas():
    """La serie termina en `hasta` con la última vela valorada; su patrimonio es el de la contabilidad (caja + posición
    al último cierre) y la atribución por estrategia cierra con el resultado (residuo ~0)."""
    r, m = _mini_backtest((100.0, 110.0, 95.0, 104.0), n_despues=1, est=_EstMantener())
    assert r.serie.index[-1] == T0 + 3 * H4, r.serie.index[-1]
    st = r.db.get("papel")
    contable = st["caja"] + sum(p["c"] * (100.0 - p["px"]) for p in st["pos"].values())       # último cierre: 100
    assert abs(r.serie.E.iat[-1] - contable) < 1e-9, (r.serie.E.iat[-1], contable)
    a = r.info["atribucion"]
    assert abs(a["residuo_principal"]) < 1e-6 and abs(a["total"] - (r.serie.E.iat[-1] - 1000.0)) < 1e-6, a


PRUEBAS = [v01a_stop_con_salto_queda_dentro_de_la_medida, v01b_ganancia_posterior_al_stop_no_cuenta,
           v01c_ultima_vela_valorada_y_cuentas_cerradas,
           v04a_reinicio_con_estado_terminal_sin_aplicar, v04b_caida_entre_dos_escrituras_del_libro,
           v04c_orden_abierta_que_llena_tarde, v04d_pendiente_bloquea_aumentos,
           v05a_opuestos_desde_plano, v05b_prioridad_decide_el_lado, v05c_cambio_de_lado_en_el_mismo_ciclo,
           v06a_respuesta_perdida_no_duplica, v06b_sin_respuesta_y_sin_confirmar_queda_incierta,
           v06c_servicio_bloquea_balas_mientras_hay_incierta,
           v03a_apertura_real_que_falla_no_inventa_estado, v03b_apertura_a_medias_se_deshace,
           v03c_respuesta_perdida_tras_abrir_toma_lo_real, v03d_patrimonio_y_nocional_son_los_reales, o15b_recarga_solo_margen_real,
           v02a_la_reserva_no_protege_una_vela_pasada, v02b_reserva_decidida_antes_protege_la_vela_siguiente,
           v07a_funding_de_balas_es_el_del_inverso, v07b_funding_por_evento_con_la_posicion_de_ese_momento,
           v07c_funding_despues_de_los_stops_en_papel, v07d_stop_con_deslizamiento,
           v09_balas_sin_costos_ni_funding_inventado,
           v03e_orden_sin_estado_terminal_no_se_da_por_hecha, v03f_cierre_verificado, v03g_transferencia_interna_con_respuesta_perdida]


def main():
    import logging
    import cascada.principal as PR
    import cascada.subcuenta as SC
    logging.disable(logging.CRITICAL)
    import cascada.balas_real as BR
    SC.time.sleep = PR.time.sleep = BR.time.sleep = lambda *_: None      # sin esperas en las pruebas
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
