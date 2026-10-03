"""Pruebas del servicio más allá de los 15 escenarios de la auditoría (plan, fase 0.6):
cortes de red al enviar órdenes, reinicio a mitad de ciclo, velas y precios faltantes, volumen de los perpetuos,
capital de 30 balas por campaña y funding del papel.

    python -m tests.test_servicio
"""
import sys
import traceback

import pandas as pd

from cascada import config as C
from cascada.datos import Datos, VolumenPerp
from cascada.db import Base
from cascada.motor import Motor

from .test_auditoria import H4, T0, FakeBolsa, FakeDatos, FakeEst, FakePub, ciclo, libro_neto, lote, montar


class BolsaRed(FakeBolsa):
    """Exchange cuya red se corta: `cortar_despues` llena la orden y después falla la respuesta; `cortar_antes` falla
    sin que la orden llegue. Mientras `caida`, también fallan las consultas."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.cortar_despues = 0; self.cortar_antes = 0; self.caida = False

    def enviar_orden(self, base, lado, contratos, reduce_only=False, client_oid=None, precio_ref=None):
        if self.cortar_antes > 0:
            self.cortar_antes -= 1; self.caida = True
            raise TimeoutError("red caída antes de llegar")
        r = super().enviar_orden(base, lado, contratos, reduce_only, client_oid, precio_ref)
        if self.cortar_despues > 0:
            self.cortar_despues -= 1; self.caida = True
            raise TimeoutError("red caída después de llenar")
        return r

    def estado_orden(self, base, client_oid):
        if self.caida:
            raise TimeoutError("sin red")
        return super().estado_orden(base, client_oid)


def montar_red(caja=1000.0, precio=10.0):
    db = Base(":memory:")
    b = BolsaRed(caja)
    b.fijar_precios({"BTC": precio, "ETH": precio})
    pub = FakePub(b)
    cfg = C.Config(pesos={"fake": 1.0}, prioridad=["fake"])
    m = Motor(cfg, db, FakeDatos(), b, pub)
    e = FakeEst(); m.est = {"fake": e}
    return m, b, e, db, pub, cfg


def compras(b, base="BTC"):
    return [o for o in b.ordenes if o["base"] == base and o["llenado"] > 0]


# ------------------------------------------------------------------ red
def r01_red_cae_despues_de_llenar():
    """La orden se llena pero la respuesta se pierde: no se duplica y el libro se recupera en el ciclo siguiente."""
    m, b, e, db, *_ = montar_red()
    e.deseos = [lote(frac=0.1)]
    b.cortar_despues = 1
    ciclo(m, T0)
    assert b.posiciones() == {"BTC": 10}, b.posiciones()
    assert not m.libro(), "el libro no debe inventar un lote sin confirmación"
    assert db.filas("SELECT estado FROM ordenes")[0]["estado"] == "incierta"
    b.caida = False
    ciclo(m, T0 + H4)
    assert libro_neto(m) == b.posiciones() == {"BTC": 10}, (libro_neto(m), b.posiciones())
    assert len(compras(b)) == 1, f"se compró {len(compras(b))} veces"


def r02_red_cae_antes_de_llegar():
    """La orden nunca llegó: mientras está incierta no se manda otra; si no aparece, se da por perdida y se avisa;
    libro y exchange siguen iguales en todo momento."""
    m, b, e, db, *_ = montar_red()
    e.deseos = [lote(frac=0.1)]
    b.cortar_antes = 1
    ciclo(m, T0)
    b.caida = False
    for k in range(1, 5):
        ciclo(m, T0 + k * H4)
        assert libro_neto(m) == b.posiciones(), (k, libro_neto(m), b.posiciones())
    est = [o["estado"] for o in db.filas("SELECT estado FROM ordenes ORDER BY ts")]
    assert est[0] == "perdida", est
    assert db.filas("SELECT 1 FROM incidencias WHERE tipo='orden_perdida'"), "falta el aviso de orden perdida"
    assert len(compras(b)) <= 1


# ------------------------------------------------------------------ reinicio
def r03_reinicio_a_mitad_de_ciclo():
    """El proceso muere después de que el exchange llenó y antes de anotar: al reiniciar, la orden se encuentra por
    clientOid, el lote queda registrado y con su stop."""
    m, b, e, db, pub, cfg = montar_red()
    e.deseos = [lote(frac=0.1, reesc=False, stop_dist=1.0)]
    original = m._cerrar_orden

    def muere(*a, **k):
        raise KeyboardInterrupt("proceso terminado")
    m._cerrar_orden = muere
    try:
        m.ciclo(T0, precios=dict(b.ultimo), velas_cerradas={})
    except KeyboardInterrupt:
        pass
    assert b.posiciones() == {"BTC": 10} and not m.libro()
    m2 = Motor(cfg, db, FakeDatos(), b, pub)           # servicio nuevo sobre la misma base
    e2 = FakeEst(); e2.deseos = [lote(frac=0.1, reesc=False, stop_dist=1.0)]; m2.est = {"fake": e2}
    ciclo(m2, T0 + H4)
    assert libro_neto(m2) == b.posiciones() == {"BTC": 10}, (libro_neto(m2), b.posiciones())
    assert sum(s["c"] for s in b.stops_vivos("BTC")) >= 10, "la posición quedó sin stop"
    assert len(compras(b)) == 1


def r04_reinicio_sin_cambios_no_opera():
    """Un reinicio sin nada pendiente no manda órdenes ni duplica stops."""
    m, b, e, db, pub, cfg = montar_red()
    e.deseos = [lote(frac=0.1, reesc=False, stop_dist=1.0)]
    ciclo(m, T0)
    n_ord, n_stops = len(b.ordenes), len(b.stops_vivos("BTC"))
    m2 = Motor(cfg, db, FakeDatos(), b, pub)
    e2 = FakeEst(); e2.deseos = [lote(frac=0.1, reesc=False, stop_dist=1.0)]; m2.est = {"fake": e2}
    ciclo(m2, T0 + H4)
    assert len(b.ordenes) == n_ord, "un reinicio no debería operar"
    assert len(b.stops_vivos("BTC")) == n_stops, (len(b.stops_vivos("BTC")), n_stops)


# ------------------------------------------------------------------ velas y precios faltantes
def r05_vela_faltante():
    """Sin la vela cerrada de un símbolo, el ciclo sigue: no revisa ese stop, no rompe; con la vela, el stop corre."""
    m, b, e, db = montar()
    e.deseos = [lote(frac=0.1, reesc=False, stop_dist=1.0)]
    ciclo(m, T0)
    r = ciclo(m, T0 + H4, velas={})
    assert "error" not in (r or {}), r
    assert b.posiciones() == {"BTC": 10}
    b.fijar_precios({"BTC": 8.5})
    r = ciclo(m, T0 + 2 * H4, velas={"BTC": (9.5, 9.6, 8.4)})
    assert "error" not in (r or {}), r
    assert not b.posiciones() and not m.libro(), (b.posiciones(), m.libro())


def r06_precio_faltante():
    """Un símbolo sin precio en el ciclo no se opera y el resto sigue."""
    m, b, e, db = montar()
    e.deseos = [lote("a", frac=0.1, sim="BTC"), lote("b", frac=0.1, sim="ETH")]
    precios = {"BTC": 10.0}
    try:
        m.ciclo(T0, precios=precios, velas_cerradas={})
    except Exception as ex:
        raise AssertionError(f"el ciclo rompió sin precio de ETH: {ex!r}")
    assert "ETH" not in b.posiciones() and b.posiciones().get("BTC") == 10, b.posiciones()


# ------------------------------------------------------------------ volumen de los perpetuos
class FakeBinance:
    def __init__(self):
        self.pedidos = []

    def load_markets(self):
        return {"BTC/USDT:USDT": dict(base="BTC", id="BTCUSDT", swap=True, linear=True, quote="USDT"),
                "1000PEPE/USDT:USDT": dict(base="1000PEPE", id="1000PEPEUSDT", swap=True, linear=True, quote="USDT")}

    def fapiPublicGetKlines(self, p):
        self.pedidos.append(p["symbol"])
        hoy = pd.Timestamp.now("UTC").tz_localize(None).normalize()
        dias = pd.date_range(end=hoy, periods=p["limit"], freq="D")
        v = 5e6 if p["symbol"] == "BTCUSDT" else 1e6
        return [[int(d.value // 10**6), "1", "1", "1", "1", "0", 0, str(v)] for d in dias]


class FakeKucoinPub:
    def mercados(self, refrescar=False):
        return {"ABC": dict(tam=10.0, minimo=1, id="ABCUSDTM")}

    def velas(self, base, tf, desde, hasta=None):
        dias = pd.date_range(pd.to_datetime(desde, unit="ms"), pd.to_datetime(hasta, unit="ms") - pd.Timedelta(days=1), freq="D")
        return pd.DataFrame(dict(o=1.0, h=1.0, l=1.0, c=2.0, v=1000.0), index=dias)


def r07_volumen_del_perpetuo():
    """El filtro de liquidez usa el volumen USDT del perpetuo: Binance (con prefijo 1000) o KuCoin; reemplaza al de CMC."""
    db = Base(":memory:")
    db.ejec("INSERT INTO vol_cmc VALUES ('2026-01-01','BTC',1.0)")        # dato viejo de CMC
    d = Datos(db, None)
    f = VolumenPerp(FakeKucoinPub(), binance=FakeBinance())
    t = pd.Timestamp.now("UTC").tz_localize(None).floor("4h")
    ok, faltan = d.registrar_volumen(f, ["BTC", "PEPE", "ABC", "NOEXISTE"], t)
    assert ok and faltan == ["NOEXISTE"], (ok, faltan)
    assert not db.filas("SELECT 1 FROM vol_cmc WHERE fecha='2026-01-01'"), "quedó el volumen viejo de CMC"
    vol, ndias = d.volumenes(t)
    assert ndias >= 29, ndias
    assert vol["BTC"] == 5e6 and vol["PEPE"] == 1e6 and vol["ABC"] == 1000 * 10 * 2, vol
    ok2, _ = d.registrar_volumen(f, ["BTC"], t)
    assert not ok2, "el mismo día no se vuelve a bajar"


# ------------------------------------------------------------------ servicio en papel: balas y funding
class PubServicio:
    def __init__(self):
        self.tasas = []

    def mercados(self, refrescar=False):
        return {"BTC": dict(tam=0.001, minimo=1, id="XBTUSDTM"), "ETH": dict(tam=0.01, minimo=1, id="ETHUSDTM")}

    def precios(self, bases):
        return {"BTC": 50000.0, "ETH": 3000.0}

    def funding(self, base):
        return 0.0001

    def funding_liquidado(self, base, desde_ms):
        return [x for x in self.tasas if x[0] > desde_ms]


def _sistema():
    from cascada.principal import Sistema
    cfg = C.Config(pesos=dict(C.NIVELES["nivel_30"]), nivel="nivel_30", capital_papel=3000.0)
    pub = PubServicio()
    return Sistema(cfg=cfg, avisar=lambda *a: None, publico=pub, ruta_db=":memory:", publico_control=pub), pub


def r08_capital_de_balas_por_campaña():
    """Como en la validación (E4): sin campaña abierta, 30 balas tiene peso × patrimonio total; con campaña, no se toca;
    el patrimonio total no cambia por la transferencia."""
    s, pub = _sistema()
    s.bolsa.fijar_precios(pub.precios([]))
    s.bolsa.st["caja"] += 300.0                      # la cuenta principal ganó 300
    total = s.bolsa.patrimonio() + s.balas.patrimonio(50000.0)
    s.capital_balas(50000.0)
    assert abs(s.balas.st["W"] - 0.34875 * total) < 1e-6, (s.balas.st["W"], total)
    assert abs(s.bolsa.patrimonio() + s.balas.patrimonio(50000.0) - total) < 1e-6
    s.balas.st["activo"] = True; W = s.balas.st["W"]
    s.bolsa.st["caja"] += 500.0
    s.capital_balas(50000.0)
    assert s.balas.st["W"] == W, "durante la campaña el capital de balas no se toca"


def r09_funding_en_papel():
    """El papel cobra y paga el funding liquidado de cada posición (el largo paga si la tasa es positiva)."""
    s, pub = _sistema()
    s.bolsa.fijar_precios(pub.precios([]))
    s.bolsa.st["pos"]["BTC"] = dict(c=10.0, px=50000.0)       # 10 contratos × 0,001 BTC = 0,01 BTC = 500 USDT
    t1 = pd.Timestamp("2026-10-03 08:00")
    s.funding_papel(t1)                                         # la primera vez sólo marca desde cuándo
    caja = s.bolsa.st["caja"]
    pub.tasas = [(int(t1.value // 10**6) + 1000, 0.0001), (int((t1 + pd.Timedelta(hours=8)).value // 10**6), 0.0002)]
    s.funding_papel(t1 + pd.Timedelta(hours=8))
    assert abs((caja - s.bolsa.st["caja"]) - 500 * 0.0003) < 1e-9, caja - s.bolsa.st["caja"]
    s.funding_papel(t1 + pd.Timedelta(hours=12))                # no se cobra dos veces
    assert abs((caja - s.bolsa.st["caja"]) - 500 * 0.0003) < 1e-9


def r10_servicio_usa_la_config_validada():
    """El servicio arma el tope con el nocional real de balas y el papel con el deslizamiento de la validación."""
    s, pub = _sistema()
    assert s.cfg.tope_con_balas_real and s.motor.balas_nocional is not None
    assert s.bolsa.desliz == {"BTC": 0.0005, "ETH": 0.0005, "_": 0.0010}, s.bolsa.desliz


PRUEBAS = [r01_red_cae_despues_de_llenar, r02_red_cae_antes_de_llegar, r03_reinicio_a_mitad_de_ciclo,
           r04_reinicio_sin_cambios_no_opera, r05_vela_faltante, r06_precio_faltante, r07_volumen_del_perpetuo,
           r08_capital_de_balas_por_campaña, r09_funding_en_papel, r10_servicio_usa_la_config_validada]


def main():
    import logging
    logging.disable(logging.CRITICAL)        # las fallas provocadas a propósito dejan trazas en el log
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
