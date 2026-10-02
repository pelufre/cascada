"""Pruebas de los 15 defectos operativos de la auditoría (O01–O15).

Cada prueba arma la situación que describe la auditoría y comprueba el comportamiento correcto. Sin internet ni pytest:
    python -m tests.test_auditoria
"""
import sys
import traceback

import numpy as np
import pandas as pd

from cascada import config as C
from cascada.db import Base
from cascada.motor import Motor

T0 = pd.Timestamp("2026-01-06 04:00")      # martes
H4 = pd.Timedelta(hours=4)


# ------------------------------------------------------------------ dobles de prueba
class FakeBolsa:
    """Exchange de prueba con fallas programables. Implementa la interfaz nueva y la vieja (para ver las pruebas en rojo
    contra el código anterior)."""
    modo = "papel"

    def __init__(self, caja=1000.0, comision=0.0):
        self.caja = caja; self.com = comision
        self.pos = {}; self.ultimo = {}; self.stops = {}; self.hechas = {}; self.ordenes = []
        self.llenar_solo = []        # próximos llenados forzados (contratos)
        self.rechazar_stops = 0      # cuántos stops rechazar
        self.rechazar_ordenes = 0    # cuántas órdenes rechazar
        self.n = 0

    # ---- estado
    def fijar_precios(self, p):
        self.ultimo.update(p)

    def patrimonio(self):
        return self.caja + sum(c * (self.ultimo.get(b, px) - px) for b, (c, px) in self.pos.items())

    def posiciones(self):
        return {b: c for b, (c, px) in self.pos.items() if c}

    def _llenar(self, base, delta, px):
        c, p = self.pos.get(base, (0.0, px))
        nuevo = c + delta
        if c == 0 or (c > 0) == (delta > 0):
            p = (c * p + delta * px) / nuevo
        else:
            cerrado = -delta if abs(delta) <= abs(c) else c
            self.caja += cerrado * (px - p)
            if abs(delta) > abs(c):
                p = px
        if abs(nuevo) < 1e-12:
            self.pos.pop(base, None)
        else:
            self.pos[base] = (nuevo, p)

    # ---- interfaz nueva
    def enviar_orden(self, base, lado, contratos, reduce_only=False, client_oid=None, precio_ref=None):
        self.n += 1
        oid = client_oid or f"o{self.n}"
        actual = self.pos.get(base, (0.0, 0.0))[0]
        sg = 1 if lado == "buy" else -1
        q = float(contratos)
        if self.rechazar_ordenes > 0:
            self.rechazar_ordenes -= 1
            q = 0.0
        elif self.llenar_solo:
            q = min(q, self.llenar_solo.pop(0))
        if reduce_only:
            q = 0.0 if actual == 0 or (actual > 0) == (sg > 0) else min(q, abs(actual))
        px = self.ultimo[base]
        if q > 0:
            self._llenar(base, sg * q, px)
        com = q * px * self.com
        self.caja -= com
        r = dict(estado="cerrada" if q == contratos else "cancelada" if q > 0 else "rechazada", llenado=q,
                 precio=px if q > 0 else None, comision=com, orden_id=oid)
        self.ordenes.append(dict(base=base, lado=lado, c=contratos, reduce=bool(reduce_only), llenado=q, actual=actual))
        self.hechas[oid] = r
        return r

    def estado_orden(self, base, client_oid):
        return self.hechas.get(client_oid)

    def enviar_stop(self, base, lado, contratos, precio, client_oid=None):
        if self.rechazar_stops > 0:
            self.rechazar_stops -= 1
            raise RuntimeError("stop rechazado")
        self.n += 1
        oid = f"s{self.n}"
        self.stops[oid] = dict(base=base, lado=lado, c=contratos, px=precio, estado="abierta", llenado=0.0, precio=None)
        return oid

    def estado_stop(self, base, oid):
        s = self.stops.get(oid)
        if not s:
            return dict(estado="desconocida", llenado=0.0, precio=None)
        return dict(estado=s["estado"], llenado=s["llenado"], precio=s["precio"])

    def cancelar_stop(self, base, oid):
        s = self.stops.get(oid)
        if s and s["estado"] == "abierta":
            s["estado"] = "cancelada"
        return True

    def revisar_stops(self, velas):
        for s in self.stops.values():
            v = velas.get(s["base"])
            if s["estado"] != "abierta" or v is None:
                continue
            o, h, l = v[:3]
            if s["lado"] == "sell" and l <= s["px"]:
                px = min(o, s["px"])
            elif s["lado"] == "buy" and h >= s["px"]:
                px = max(o, s["px"])
            else:
                continue
            actual = self.pos.get(s["base"], (0.0, 0.0))[0]
            q = min(s["c"], abs(actual))
            self._llenar(s["base"], q if s["lado"] == "buy" else -q, px)
            s.update(estado="ejecutada", llenado=q, precio=px)

    def stops_vivos(self, base=None):
        return [s for s in self.stops.values() if s["estado"] == "abierta" and (base is None or s["base"] == base)]

    # ---- interfaz vieja
    def orden_mercado(self, base, lado, contratos, reduce_only=False, client_oid=None):
        r = self.enviar_orden(base, lado, contratos, reduce_only, client_oid)
        if r["estado"] == "rechazada":
            raise RuntimeError("orden rechazada")
        return dict(id=r["orden_id"], precio=r["precio"], contratos=r["llenado"], comision=r["comision"])

    def orden_stop(self, base, lado, contratos, precio, client_oid=None):
        return self.enviar_stop(base, lado, contratos, precio, client_oid)

    def stops_abiertos(self, bases=None):
        return {i for i, s in self.stops.items() if s["estado"] == "abierta"}

    def precio_stop(self, base, oid, desde):
        s = self.stops.get(oid)
        return s and s["precio"]


class FakePub:
    def __init__(self, bolsa):
        self.bolsa = bolsa
        self.m = {"BTC": dict(tam=1.0, minimo=1.0, id="BTC"), "ETH": dict(tam=1.0, minimo=1.0, id="ETH")}

    def mercados(self, refrescar=False):
        return self.m

    def precios(self, bases):
        return {b: self.bolsa.ultimo[b] for b in bases if b in self.bolsa.ultimo}


class FakeDatos:
    def universo(self, t):
        return []


class FakeEst:
    nombre = "fake"
    tf = "4h"

    def __init__(self):
        self.deseos = []

    def decide_en(self, t):
        return True

    def paso(self, t, datos, st, abiertos, universo, mercados):
        return [dict(x) for x in self.deseos]


def montar(caja=1000.0, precio=10.0, comision=0.0):
    db = Base(":memory:")
    b = FakeBolsa(caja, comision)
    b.fijar_precios({"BTC": precio, "ETH": precio})
    pub = FakePub(b)
    cfg = C.Config(pesos={"fake": 1.0}, prioridad=["fake"])
    m = Motor(cfg, db, FakeDatos(), b, pub)
    e = FakeEst()
    m.est = {"fake": e}
    return m, b, e, db


def frac_para(contratos, precio, E):
    return contratos * precio / E * (1 + 1e-6)


def lote(i="a", frac=0.1, lado=1, reesc=True, stop_dist=None, sim="BTC"):
    d = dict(id=i, simbolo=sim, lado=lado, frac=frac, reescalable=reesc)
    if stop_dist:
        d["stop_dist"] = stop_dist
    return d


def ciclo(m, t, precio=None, velas=None):
    b = m.bolsa
    if precio is not None:
        b.fijar_precios({"BTC": precio})
    try:
        return m.ciclo(t, precios=dict(b.ultimo), velas_cerradas=velas or {})
    except Exception as ex:      # el código viejo puede romper; la prueba mira el estado que quedó
        return dict(error=repr(ex))


def libro_neto(m):
    out = {}
    for L in m.libro().values():
        out[L["simbolo"]] = out.get(L["simbolo"], 0) + L["lado"] * abs(L["contratos"])
    return {k: v for k, v in out.items() if v}


# ------------------------------------------------------------------ pruebas
def o01_llenado_parcial():
    """Se pidieron 10 contratos y el exchange llenó 3: el libro debe registrar 3."""
    m, b, e, db = montar()
    e.deseos = [lote(frac=0.1)]
    b.llenar_solo = [3]
    ciclo(m, T0)
    L = m.libro().get("a")
    assert b.posiciones() == {"BTC": 3}, b.posiciones()
    assert L is not None and abs(L["contratos"]) == 3, f"libro {L and L['contratos']} / exchange 3"


def o02_stop_rechazado():
    """Si el stop no se puede colocar, la posición no puede quedar abierta sin protección."""
    m, b, e, db = montar()
    e.deseos = [lote(frac=0.1, reesc=False, stop_dist=1.0)]
    b.rechazar_stops = 2
    ciclo(m, T0)
    pos = b.posiciones().get("BTC", 0)
    protegido = sum(s["c"] for s in b.stops_vivos("BTC"))
    assert pos == 0 or protegido >= abs(pos), f"posición {pos} con stops por {protegido}"


def o03_reducciones_reduce_only():
    """Toda orden que achica o cierra una posición va reduce-only."""
    m, b, e, db = montar()
    e.deseos = [lote(frac=0.1)]
    ciclo(m, T0)
    e.deseos = [lote(frac=0.05)]
    ciclo(m, T0 + H4)
    e.deseos = []
    ciclo(m, T0 + 2 * H4)
    red = [o for o in b.ordenes if o["actual"] and (o["actual"] > 0) != (o["lado"] == "buy")]
    assert red, "no hubo reducciones"
    assert all(o["reduce"] for o in red), [(o["lado"], o["c"], o["reduce"]) for o in red]


def o04_pausa_no_aumenta():
    """En pausa no se agranda ningún lote (tampoco los reescalables)."""
    m, b, e, db = montar()
    e.deseos = [lote(frac=0.05)]
    ciclo(m, T0)
    antes = b.posiciones().get("BTC", 0)
    db.set("pausado", True)
    e.deseos = [lote(frac=0.10), lote("b", frac=0.1, sim="ETH")]
    ciclo(m, T0 + H4)
    assert b.posiciones().get("BTC", 0) <= antes and not b.posiciones().get("ETH"), b.posiciones()


def o05_conciliar_antes():
    """Con contratos ajenos en el exchange, ese ciclo no se agranda nada."""
    m, b, e, db = montar()
    b.pos["BTC"] = (7.0, 10.0)
    e.deseos = [lote(frac=0.1)]
    ciclo(m, T0)
    assert b.posiciones().get("BTC") == 7, b.posiciones()


def o06_stop_cancelado_afuera():
    """Un stop cancelado fuera del sistema no es un stop ejecutado: el lote sigue y el stop se repone."""
    m, b, e, db = montar()
    e.deseos = [lote(frac=0.1, reesc=False, stop_dist=1.0)]
    ciclo(m, T0)
    for s in b.stops_vivos():
        s["estado"] = "cancelada"
    ciclo(m, T0 + H4)
    L = m.libro().get("a")
    assert L is not None, "el libro cerró el lote como si el stop se hubiera ejecutado"
    assert b.posiciones().get("BTC") == L["lado"] * abs(L["contratos"])
    assert sum(s["c"] for s in b.stops_vivos("BTC")) >= abs(L["contratos"]), "no repuso el stop"


def o07_tope_y_conciliacion():
    """Después de recortar por tope, libro y exchange coinciden y la conciliación queda bien."""
    m, b, e, db = montar()
    e.deseos = [lote(frac=0.9, lado=-1, reesc=False)]
    ciclo(m, T0)
    assert b.posiciones().get("BTC") == -90, b.posiciones()
    ciclo(m, T0 + H4, precio=12.0)          # nocional 1080 > 1,05 × 820
    n = abs(b.posiciones().get("BTC", 0)) * 12
    assert n <= 1.05 * b.patrimonio() + 12, f"no recortó: nocional {n}"
    assert libro_neto(m) == b.posiciones(), (libro_neto(m), b.posiciones())
    assert db.get("conciliacion_ok") is True, f"conciliación {db.get('conciliacion_ok')}"


def o08_pnl_acumulado():
    """Abrir 10 a 100, bajar a 5 a 110 (+50) y cerrar a 120 (+100): resultado 150."""
    m, b, e, db = montar(caja=2000.0, precio=100.0)
    e.deseos = [lote(frac=frac_para(10, 100, 2000))]
    ciclo(m, T0)
    e.deseos = [lote(frac=frac_para(5, 110, 2100))]
    ciclo(m, T0 + H4, precio=110.0)
    assert b.posiciones().get("BTC") == 5, b.posiciones()
    e.deseos = []
    ciclo(m, T0 + 2 * H4, precio=120.0)
    r = db.filas("SELECT pnl FROM lotes WHERE id='a'")[0]["pnl"]
    assert abs(r - 150) < 1e-6, f"pnl {r}"


def o09_papel_reduce_only():
    """El exchange simulado respeta reduce-only: sin posición, una venta reduce-only no abre un corto."""
    from cascada.bolsa import Papel
    db = Base(":memory:")
    p = Papel(db, {"BTC": dict(tam=1.0, minimo=1.0)}, capital=1000)
    p.fijar_precios({"BTC": 10.0})
    if hasattr(p, "enviar_orden"):
        p.enviar_orden("BTC", "sell", 5, reduce_only=True)
    else:
        p.orden_mercado("BTC", "sell", 5, True)
    assert not p.posiciones(), p.posiciones()
    if hasattr(p, "enviar_orden"):
        p.enviar_orden("BTC", "buy", 3)
        p.enviar_orden("BTC", "sell", 5, reduce_only=True)
    else:
        p.orden_mercado("BTC", "buy", 3); p.orden_mercado("BTC", "sell", 5, True)
    assert not p.posiciones(), f"reduce-only cruzó a {p.posiciones()}"


def _serie_btc(n=1500, fin=T0):
    idx = pd.date_range(end=fin - H4, periods=n, freq="4h")
    rng = np.random.default_rng(1)
    c = 30000 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, n)))
    return pd.Series(c, idx)


def o10_balas_con_historia():
    """30 balas arranca con la historia: cierres diarios, EMA semanal y momento listos desde el primer ciclo."""
    from cascada.balas import Balas
    db = Base(":memory:")
    bl = Balas(db, 600.0)
    s = _serie_btc()
    c = s.iat[-1]
    bl.procesar(T0, (c, c, c, c), s, funding_8h=0.0001)
    st = bl.st
    assert len(st["dC"]) >= 200, f"dC {len(st['dC'])}"
    assert len(st["emaHist"]) >= 5, f"emaHist {len(st['emaHist'])}"


def o11_corte_reintenta():
    """Si el cierre por corte falla, el sistema sigue intentando hasta quedar plano y bloqueado."""
    m, b, e, db = montar()
    e.deseos = [lote(frac=0.5)]
    ciclo(m, T0)
    assert b.posiciones().get("BTC") == 50
    db.set("maximo_patrimonio", 3000.0)    # caída de 67 %: corte
    b.rechazar_ordenes = 1
    ciclo(m, T0 + H4)
    ciclo(m, T0 + 2 * H4)
    assert not b.posiciones(), f"sigue abierto: {b.posiciones()}"
    assert db.get("bloqueado") is True


class _DatosRSI:
    def v4(self, base, t, dias=None):
        idx = pd.date_range(end=pd.Timestamp(t) - H4, periods=400, freq="4h")
        return pd.DataFrame(dict(c=np.linspace(100, 200, 400)), index=idx)   # sube siempre: RSI alto, sin señal


def o12_rsi2_no_reentra():
    """RSI(2) cuyo lote cerró otro mecanismo (tope, corte, manual) espera una señal nueva para volver a entrar."""
    from cascada.estrategias import RSI2
    st = dict(dentro=True, armado=False, id="rsi2_btc_x", _cerrados={"rsi2_btc_x": "tope"})
    out = RSI2("BTC").paso(T0, _DatosRSI(), st, {}, [], {})
    assert out == [], out


def o12b_rsi2_sigue_si_fue_reparto():
    """Si el lote se cerró porque el reparto le dio 0 (no por un mecanismo externo), RSI(2) sigue adentro como en el
    backtest y vuelve a pedir capital con un lote nuevo."""
    from cascada.estrategias import RSI2
    st = dict(dentro=True, armado=False, id="rsi2_btc_x", _cerrados={"rsi2_btc_x": "asignacion"})
    out = RSI2("BTC").paso(T0, _DatosRSI(), st, {}, [], {})
    assert len(out) == 1 and out[0]["id"] != "rsi2_btc_x", out


class _ExKucoin:
    """ccxt falso: la orden queda abierta con 3 de 10 llenados hasta que se cancela."""
    def __init__(self):
        self.cancelada = False; self.llamadas = []

    def create_order(self, sym, tipo, lado, c, precio=None, params=None):
        self.llamadas.append(("create", tipo, lado, c, dict(params or {})))
        return dict(id="X1")

    def fetch_order(self, oid, sym=None, params=None):
        return dict(id="X1", status="canceled" if self.cancelada else "open", filled=3.0, amount=10.0, average=100.0,
                    fee=dict(cost=0.18))

    def cancel_order(self, oid, sym=None, params=None):
        self.llamadas.append(("cancel", oid)); self.cancelada = True
        return {}

    def load_markets(self, *a):
        return {}


def o13_kucoin_parcial():
    """Con llenado parcial en KuCoin se cancela el resto y se informa sólo lo llenado."""
    import cascada.bolsa as B
    ex = _ExKucoin()
    try:
        k = B.KucoinReal({}, None, ex=ex)
    except TypeError:
        k = B.KucoinReal.__new__(B.KucoinReal); k.ex = ex; k.pub = None; k.lev = 3
    if hasattr(B.KucoinReal, "ESPERA"):
        k.ESPERA = 0.0
    import time as _t
    dormir = _t.sleep; _t.sleep = lambda s: None
    try:
        r = k.enviar_orden("BTC", "buy", 10, False, "c1") if hasattr(k, "enviar_orden") else k.orden_mercado("BTC", "buy", 10)
    finally:
        _t.sleep = dormir
    llen = r.get("llenado", r.get("contratos"))
    assert llen == 3, f"informó {llen}"
    assert any(x[0] == "cancel" for x in ex.llamadas), "no canceló el resto de la orden"


def o14_lote_a_cero():
    """Un lote reescalable cuyo objetivo cae a 0 contratos se cierra (no queda abierto con 0)."""
    m, b, e, db = montar()
    e.deseos = [lote(frac=0.1)]
    ciclo(m, T0)
    e.deseos = [lote(frac=0.0001)]
    ciclo(m, T0 + H4)
    assert "a" not in m.libro(), "lote abierto con 0 contratos"
    assert not b.posiciones(), b.posiciones()


class _RealBalas:
    def __init__(self):
        self.llamadas = []

    def __getattr__(self, n):
        return lambda *a, **k: self.llamadas.append((n, a))


def o15_recarga_solo_margen():
    """Una recarga que sólo agrega margen (sin contratos) también se ejecuta en la subcuenta real."""
    from cascada.balas import Balas
    db = Base(":memory:")
    real = _RealBalas()
    bl = Balas(db, 600.0, real=real)
    t = pd.Timestamp("2026-01-07 00:00")      # miércoles 00:00
    s = bl.st
    s.update(activo=True, rec=False, resd=False, used=5, ntn=1.0, inv=0.01, mbtc=0.02, contrib=0.1, regWeekly=True,
             dC=[60.0] * 250, emaHist=[50.0] * 6, emaW=50.0, momT=True, calentado=True, entry_t=str(t - 10 * H4))
    cierres = pd.Series(np.linspace(70, 62, 100), index=pd.date_range(end=t - H4, periods=100, freq="4h"))
    bl.procesar(t, (62.0, 62.0, 62.0, 62.0), cierres, funding_8h=0.0)
    assert s["used"] > 5, "no hubo recarga en la simulación"
    assert real.llamadas, "la subcuenta real no recibió el margen"


PRUEBAS = [o01_llenado_parcial, o02_stop_rechazado, o03_reducciones_reduce_only, o04_pausa_no_aumenta, o05_conciliar_antes,
           o06_stop_cancelado_afuera, o07_tope_y_conciliacion, o08_pnl_acumulado, o09_papel_reduce_only,
           o10_balas_con_historia, o11_corte_reintenta, o12_rsi2_no_reentra, o12b_rsi2_sigue_si_fue_reparto, o13_kucoin_parcial, o14_lote_a_cero,
           o15_recarga_solo_margen]


def main():
    import logging
    logging.disable(logging.CRITICAL)
    mal = 0
    for f in PRUEBAS:
        try:
            f()
            print(f"✔ {f.__name__}")
        except AssertionError as ex:
            mal += 1
            print(f"✘ {f.__name__}: {ex}")
        except Exception:
            mal += 1
            print(f"✘ {f.__name__}: ERROR\n    " + traceback.format_exc().strip().splitlines()[-1])
    print(f"\n{len(PRUEBAS) - mal}/{len(PRUEBAS)} en verde")
    return mal


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
