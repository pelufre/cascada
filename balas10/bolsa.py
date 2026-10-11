"""KuCoin futuros inversos XBTUSDM (1 contrato = 1 USD, margen en BTC) con margen AISLADO, y un exchange simulado
con la misma interfaz para el modo papel y las pruebas.

Separación de capital con el Sistema 3 (misma cuenta): el Sistema 3 sólo mira USDT (spot y futuros) y posiciones
USDT-M. Este módulo sólo toca BTC de la cuenta de futuros y la posición XBTUSDM. Nunca compra ni vende en spot ni
mueve USDT.

Interfaz:
  posicion() -> dict(contratos, entrada, liq, marca, margen, pnl, modo, auto_margen) | None   (margen y pnl en BTC)
  cuenta() -> dict(equity, libre)  BTC de la cuenta de futuros
  precio() -> último precio de XBTUSDM
  comprar(contratos, apalancamiento) -> dict(contratos, precio, orden)
  cerrar_todo() -> dict(contratos, precio, orden) | None    (reduce-only)
  agregar_margen(btc) -> btc agregado
  auto_margen(False)  apaga el depósito automático de margen de KuCoin
"""
import logging
import math
import time
import uuid

log = logging.getLogger("balas10.bolsa")
SIMBOLO = "XBTUSDM"
TERMINALES = ("closed", "canceled", "cancelled", "rejected", "expired")


class SinConfirmar(RuntimeError):
    pass


def _f(x, defecto=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return defecto


class KucoinInverso:
    modo = "real"
    INTENTOS = 15
    ESPERA = 1.0

    def __init__(self, cred, fut=None):
        if fut is None:
            import ccxt
            fut = ccxt.kucoinfutures({**cred, "enableRateLimit": True})
        self.fut = fut
        self._sym = None

    def sym(self):
        if self._sym is None:
            m = self.fut.load_markets()
            for k, d in m.items():
                if d.get("id") == SIMBOLO:
                    self._sym = k
                    break
            if not self._sym:
                raise RuntimeError("XBTUSDM no aparece en los mercados de KuCoin futuros")
        return self._sym

    # ---------------------------------------------------------------- lectura (endpoints crudos: unidades exactas)
    def posicion(self):
        r = self.fut.futuresPrivateGetPosition({"symbol": SIMBOLO})
        d = (r or {}).get("data") or {}
        q = _f(d.get("currentQty"))
        if not d or q == 0 or not d.get("isOpen", True):
            return None
        return dict(contratos=q, entrada=_f(d.get("avgEntryPrice")), liq=_f(d.get("liquidationPrice")),
                    marca=_f(d.get("markPrice")), margen=_f(d.get("posMargin")), pnl=_f(d.get("unrealisedPnl")),
                    modo=(d.get("marginMode") or ("CROSS" if d.get("crossMode") else "ISOLATED")),
                    auto_margen=bool(d.get("autoDeposit")), apalancamiento=_f(d.get("realLeverage")))

    def cuenta(self):
        r = self.fut.futuresPrivateGetAccountOverview({"currency": "XBT"})
        d = (r or {}).get("data") or {}
        return dict(equity=_f(d.get("accountEquity")), libre=_f(d.get("availableBalance")))

    def precio(self):
        t = self.fut.fetch_ticker(self.sym())
        return _f(t.get("last") or t.get("close"))

    # ---------------------------------------------------------------- órdenes
    def _esperar(self, oid):
        f = {}
        for vuelta in range(2):
            for _ in range(self.INTENTOS):
                try:
                    f = self.fut.fetch_order(oid, self.sym())
                    if f.get("status") in TERMINALES:
                        return f
                except Exception as e:
                    log.debug("consulta %s: %s", oid, e)
                time.sleep(self.ESPERA)
            if vuelta == 0:
                try:
                    self.fut.cancel_order(oid, self.sym())
                except Exception as e:
                    log.warning("cancelar %s: %s", oid, e)
        raise SinConfirmar(f"orden {oid} sin estado terminal (último {f.get('status')}, llenado {f.get('filled')})")

    def comprar(self, contratos, apalancamiento=10):
        n = int(contratos)
        if n < 1:
            return None
        o = self.fut.create_order(self.sym(), "market", "buy", n, None,
                                  {"marginMode": "isolated", "leverage": int(round(apalancamiento)), "clientOid": uuid.uuid4().hex})
        f = self._esperar(o["id"])
        return dict(contratos=_f(f.get("filled")), precio=_f(f.get("average") or f.get("price")), orden=o["id"])

    def cerrar_todo(self):
        p = self.posicion()
        if not p or p["contratos"] <= 0:
            return None
        n = int(p["contratos"])
        o = self.fut.create_order(self.sym(), "market", "sell", n, None,
                                  {"reduceOnly": True, "marginMode": "isolated", "leverage": 10, "clientOid": uuid.uuid4().hex})
        f = self._esperar(o["id"])
        return dict(contratos=_f(f.get("filled")), precio=_f(f.get("average") or f.get("price")), orden=o["id"])

    def agregar_margen(self, btc):
        # endpoint crudo: ccxt.add_margin redondea el monto a la precisión de CONTRATOS (entero) y mandaría 0
        btc = math.floor(btc * 1e8) / 1e8
        if btc < 1e-6:
            return 0.0
        self.fut.futuresPrivatePostPositionMarginDepositMargin(
            {"symbol": SIMBOLO, "margin": f"{btc:.8f}", "bizNo": uuid.uuid4().hex})
        log.info("margen +%.8f BTC", btc)
        return btc

    def auto_margen(self, activo=False):
        self.fut.futuresPrivatePostPositionMarginAutoDepositStatus({"symbol": SIMBOLO, "status": bool(activo)})


class PapelInverso:
    """Exchange simulado: largo inverso aislado, comisión taker, deslizamiento, funding y liquidación con el mínimo."""
    modo = "papel"

    def __init__(self, db, precio, btc=0.5, com=0.0006, desliz=0.0005, mmr=0.007, clave="papel_inverso"):
        self.db, self._precio = db, precio
        self.com, self.d, self.mmr, self.clave = com, desliz, mmr, clave
        st = db.get(clave) if db is not None else None
        if st is None:
            st = dict(libre=float(btc), pos=None)
        self.st = st

    def _g(self):
        if self.db is not None:
            self.db.set(self.clave, self.st)

    def precio(self):
        return float(self._precio())

    def _liq(self, p):
        return p["c"] * (1 + self.mmr) / (p["m"] + p["c"] / p["e"])

    def posicion(self):
        p = self.st["pos"]
        if not p:
            return None
        px = self.precio()
        return dict(contratos=p["c"], entrada=p["e"], liq=self._liq(p), marca=px, margen=p["m"],
                    pnl=p["c"] * (1 / p["e"] - 1 / px), modo="ISOLATED", auto_margen=False,
                    apalancamiento=p["c"] / px / max(p["m"], 1e-12))

    def cuenta(self):
        p = self.st["pos"]
        eq = self.st["libre"]
        if p:
            px = self.precio()
            eq += p["m"] + p["c"] * (1 / p["e"] - 1 / px)
        return dict(equity=eq, libre=self.st["libre"])

    def comprar(self, contratos, apalancamiento=10):
        n = int(contratos)
        if n < 1:
            return None
        px = self.precio() * (1 + self.d)
        im = n / px / apalancamiento
        fee = n / px * self.com
        if im + fee > self.st["libre"] + 1e-12:
            raise RuntimeError(f"saldo insuficiente: hace falta {im + fee:.6f} BTC, hay {self.st['libre']:.6f}")
        self.st["libre"] -= im + fee
        p = self.st["pos"]
        if p:
            p["e"] = (p["c"] + n) / (p["c"] / p["e"] + n / px)
            p["c"] += n; p["m"] += im
        else:
            self.st["pos"] = dict(c=n, e=px, m=im)
        self._g()
        return dict(contratos=n, precio=px, orden=uuid.uuid4().hex[:12])

    def cerrar_todo(self):
        p = self.st["pos"]
        if not p:
            return None
        px = self.precio() * (1 - self.d)
        pnl = p["c"] * (1 / p["e"] - 1 / px)
        fee = p["c"] / px * self.com
        self.st["libre"] += p["m"] + pnl - fee
        self.st["pos"] = None
        self._g()
        return dict(contratos=p["c"], precio=px, orden=uuid.uuid4().hex[:12])

    def agregar_margen(self, btc):
        btc = min(max(btc, 0.0), self.st["libre"])
        if btc < 1e-9 or not self.st["pos"]:
            return 0.0
        self.st["libre"] -= btc
        self.st["pos"]["m"] += btc
        self._g()
        return btc

    def auto_margen(self, activo=False):
        pass

    # --- sólo simulación
    def depositar(self, btc):
        self.st["libre"] += btc
        self._g()

    def funding(self, tasa_vela):
        p = self.st["pos"]
        if p:
            p["m"] -= p["c"] * tasa_vela / self.precio()
            self._g()

    def chequear_liquidacion(self, minimo):
        p = self.st["pos"]
        if p and minimo <= self._liq(p):
            self.st["pos"] = None
            self._g()
            return True
        return False
