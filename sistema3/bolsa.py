"""Ejecución en KuCoin: spot (largos FR20 y BTC) y futuros USDT-M (cortos Aberration), más un exchange simulado
(modo papel) con la misma interfaz.

Interfaz (símbolos = ticker base, p. ej. "SOL"):
  precios_spot(sims) -> {sim: precio};  saldos_spot() -> {moneda: cantidad} (cuenta trade, USDT incluido)
  comprar_spot(sim, usdt) / vender_spot(sim, cantidad) -> dict(cantidad, precio, usdt, comision) o None si no hay orden
      (usdt = USDT que salieron en la compra, con comisión; USDT netos que entraron en la venta)
  mercado_spot(sim) -> dict(min_cant, min_usdt) o None
  contrato(sim) -> dict(tam, minimo) o None;  precio_futuro(sim)
  posiciones_futuros() -> {sim: contratos con signo};  patrimonio_futuros() / libre_futuros() -> USDT
  vender_contratos(sim, n) (abre o agranda el corto) / comprar_contratos(sim, n) (reduce-only)
  transferir(monto, hacia="futuros"|"spot")
"""
import logging
import math
import time
import uuid

log = logging.getLogger("sistema3.bolsa")
TERMINALES = ("closed", "canceled", "cancelled", "rejected", "expired")
PREFIJOS = ("1000000", "1000", "1M")


class SinConfirmar(RuntimeError):
    """El exchange no dio un estado terminal para una orden: hay que mirar la cuenta antes de seguir."""


def _sin_prefijo(base):
    for p in PREFIJOS:
        if base.startswith(p) and len(base) > len(p) and not base[len(p)].isdigit():
            return base[len(p):]
    return base


class KucoinReal:
    modo = "real"
    INTENTOS = 15
    ESPERA = 1.0

    def __init__(self, cred, apalancamiento=3, spot=None, fut=None):
        if spot is None or fut is None:
            import ccxt
            spot = ccxt.kucoin({**cred, "enableRateLimit": True})
            fut = ccxt.kucoinfutures({**cred, "enableRateLimit": True})
        self.spot, self.fut = spot, fut
        self.lev = apalancamiento
        self._ms = None; self._mf = None
        self.ruta_ida = None; self.ruta_vuelta = None

    # ---------------------------------------------------------------- mercados
    def _mercados_spot(self):
        if self._ms is None:
            m = self.spot.load_markets(True)
            self._ms = {d["base"]: d for d in m.values() if d.get("spot") and d.get("quote") == "USDT" and d.get("active", True)}
        return self._ms

    def _mercados_fut(self):
        if self._mf is None:
            m = self.fut.load_markets(True)
            out = {}
            for d in m.values():
                if d.get("swap") and d.get("linear") and d.get("settle") == "USDT" and d.get("active", True):
                    out[d["base"]] = d
            for b, d in list(out.items()):
                out.setdefault(_sin_prefijo(b), d)
            self._mf = out
        return self._mf

    def refrescar(self):
        self._ms = None; self._mf = None

    def mercado_spot(self, sim):
        d = self._mercados_spot().get(sim)
        if not d:
            return None
        lim = d.get("limits") or {}
        return dict(min_cant=float((lim.get("amount") or {}).get("min") or 0), min_usdt=float((lim.get("cost") or {}).get("min") or 0.1))

    def contrato(self, sim):
        d = self._mercados_fut().get(sim)
        if not d:
            return None
        return dict(tam=float(d.get("contractSize") or 1), minimo=float((d.get("limits", {}).get("amount", {}) or {}).get("min") or 1),
                    sym=d["symbol"])

    # ---------------------------------------------------------------- lectura
    def precios_spot(self, sims):
        ms = self._mercados_spot()
        quiero = {ms[s]["symbol"]: s for s in sims if s in ms}
        if not quiero:
            return {}
        try:
            t = self.spot.fetch_tickers(list(quiero))
        except Exception:
            t = {}
            for sym in quiero:
                try:
                    t[sym] = self.spot.fetch_ticker(sym)
                except Exception as e:
                    log.warning("precio %s: %s", sym, e)
        return {quiero[k]: float(v.get("last") or v.get("close")) for k, v in t.items() if k in quiero and (v.get("last") or v.get("close"))}

    def precio_futuro(self, sim):
        c = self.contrato(sim)
        t = self.fut.fetch_ticker(c["sym"])
        return float(t.get("last") or t.get("close"))

    def usdt_a_trading(self):
        """El USDT que quede en la cuenta principal (main) pasa a la de trading."""
        try:
            b = self.spot.fetch_balance({"type": "main"})
            u = float(b["free"].get("USDT") or 0)
            if u > 0.01:
                self.spot.transfer("USDT", math.floor(u * 100) / 100, "main", "trade")
                log.info("USDT main→trade %.2f", u)
        except Exception as e:
            log.warning("main→trade: %s", e)

    def saldos_spot(self):
        b = self.spot.fetch_balance({"type": "trade"})
        return {k: float(v) for k, v in (b.get("total") or {}).items() if v and float(v) > 0}

    def usdt_spot_libre(self):
        b = self.spot.fetch_balance({"type": "trade"})
        return float(b["free"].get("USDT") or 0)

    def posiciones_futuros(self):
        inv = {d["symbol"]: s for s, d in self._mercados_fut().items()}
        out = {}
        for p in self.fut.fetch_positions():
            c = float(p.get("contracts") or 0)
            if c and p["symbol"] in inv:
                out[_sin_prefijo(inv[p["symbol"]])] = c if p.get("side") == "long" else -c
        return out

    def patrimonio_futuros(self):
        b = self.fut.fetch_balance({"currency": "USDT"})
        try:
            return float(b["info"]["data"]["accountEquity"])
        except Exception:
            return float((b.get("total") or {}).get("USDT") or 0)

    def libre_futuros(self):
        b = self.fut.fetch_balance({"currency": "USDT"})
        try:
            return float(b["info"]["data"]["availableBalance"])
        except Exception:
            return float((b.get("free") or {}).get("USDT") or 0)

    # ---------------------------------------------------------------- órdenes
    def _esperar(self, inst, oid, sym):
        f = {}
        for vuelta in range(2):
            for _ in range(self.INTENTOS):
                try:
                    f = inst.fetch_order(oid, sym)
                    if f.get("status") in TERMINALES:
                        return f
                except Exception as e:
                    log.warning("consulta orden %s: %s", oid, e)
                time.sleep(self.ESPERA)
            if vuelta == 0:
                try:
                    inst.cancel_order(oid, sym)
                except Exception as e:
                    log.warning("cancelar %s: %s", oid, e)
        raise SinConfirmar(f"orden {oid} en {sym} sin estado terminal (último: {f.get('status')}, llenado {f.get('filled')})")

    @staticmethod
    def _neto(f, base):
        q = float(f.get("filled") or 0)
        px = float(f.get("average") or f.get("price") or 0)
        fee = f.get("fee") or {}
        com_q = float(fee.get("cost") or 0) if fee.get("currency") == base else 0.0
        com_usdt = float(fee.get("cost") or 0) if fee.get("currency") == "USDT" else com_q * px
        return q, px, com_q, com_usdt

    def comprar_spot(self, sim, usdt):
        m = self._mercados_spot().get(sim)
        if not m:
            raise RuntimeError(f"{sim}/USDT no existe en KuCoin spot")
        usdt = math.floor(usdt * 100) / 100
        if usdt < (self.mercado_spot(sim)["min_usdt"] or 0.1):
            return None
        o = self.spot.create_market_buy_order_with_cost(m["symbol"], usdt)
        f = self._esperar(self.spot, o["id"], m["symbol"])
        q, px, com_q, com_usdt = self._neto(f, sim)
        gasto = float(f.get("cost") or q * px) + (com_usdt if (f.get("fee") or {}).get("currency") == "USDT" else 0.0)
        return dict(cantidad=q - com_q, precio=px, usdt=gasto, comision=com_usdt, orden=o["id"])

    def vender_spot(self, sim, cantidad):
        m = self._mercados_spot().get(sim)
        if not m:
            raise RuntimeError(f"{sim}/USDT no existe en KuCoin spot")
        q = float(self.spot.amount_to_precision(m["symbol"], cantidad))
        lim = self.mercado_spot(sim)
        if q <= 0 or q < lim["min_cant"]:
            return None
        o = self.spot.create_order(m["symbol"], "market", "sell", q)
        f = self._esperar(self.spot, o["id"], m["symbol"])
        qq, px, com_q, com_usdt = self._neto(f, sim)
        neto = float(f.get("cost") or qq * px) - (com_usdt if (f.get("fee") or {}).get("currency") == "USDT" else 0.0)
        return dict(cantidad=qq, precio=px, usdt=neto, comision=com_usdt, orden=o["id"])

    def _contratos(self, sim, lado, n, reduce):
        c = self.contrato(sim)
        n = int(n)
        if n < max(1, int(c["minimo"])):
            return None
        params = {"marginMode": "cross", "leverage": self.lev, "clientOid": uuid.uuid4().hex}
        if reduce:
            params["reduceOnly"] = True
        o = self.fut.create_order(c["sym"], "market", lado, n, None, params)
        f = self._esperar(self.fut, o["id"], c["sym"])
        px = float(f.get("average") or f.get("price") or 0)
        q = float(f.get("filled") or 0)
        return dict(contratos=q, precio=px, nocional=q * c["tam"] * px, comision=float((f.get("fee") or {}).get("cost") or 0), orden=o["id"])

    def vender_contratos(self, sim, n):
        return self._contratos(sim, "sell", n, False)

    def comprar_contratos(self, sim, n):
        return self._contratos(sim, "buy", n, True)

    def margen_cruzado(self, sim):
        try:
            self.fut.set_margin_mode("cross", self.contrato(sim)["sym"])
        except Exception as e:
            log.info("set_margin_mode %s: %s", sim, str(e)[:120])

    # ---------------------------------------------------------------- transferencias spot ↔ futuros
    def transferir(self, monto, hacia):
        monto = math.floor(monto * 100) / 100
        if monto < 1:
            return 0.0
        if hacia == "futuros":
            rutas = [self.ruta_ida] if self.ruta_ida else [(self.spot, "trade", "future"), (self.spot, "trade", "contract"),
                                                           (self.fut, "spot", "future"), (self.spot, "main", "future")]
        else:
            rutas = [self.ruta_vuelta] if self.ruta_vuelta else [(self.fut, "future", "trade"), (self.spot, "future", "trade"),
                                                                 (self.fut, "future", "spot"), (self.spot, "contract", "trade")]
        errores = []
        for inst, desde, a in rutas:
            try:
                inst.transfer("USDT", monto, desde, a)
                if hacia == "futuros":
                    self.ruta_ida = (inst, desde, a)
                else:
                    self.ruta_vuelta = (inst, desde, a)
                    if a == "main":
                        self.usdt_a_trading()
                log.info("transferencia %.2f USDT %s→%s", monto, desde, a)
                return monto
            except Exception as e:
                errores.append(f"{getattr(inst, 'id', '?')} {desde}→{a}: {str(e)[:150]}")
        raise RuntimeError("No pude transferir USDT a " + hacia + ":\n  " + "\n  ".join(errores))


class Papel:
    """Simulación: llena a mercado al precio de referencia ± deslizamiento y cobra comisión. Guarda su estado en la base."""
    modo = "papel"

    def __init__(self, db, precio, capital=3000.0, com_spot=0.001, com_fut=0.0006, desliz=0.001, contratos=None):
        self.db, self.precio = db, precio            # precio(sim, "spot"|"fut") -> float
        self.cs, self.cf, self.d = com_spot, com_fut, desliz
        self.contratos = contratos or {}
        st = db.get("papel")
        if st is None:
            st = dict(spot={"USDT": float(capital)}, fut_caja=0.0, fut={})
            db.set("papel", st)
        self.st = st

    def _g(self):
        self.db.set("papel", self.st)

    def refrescar(self):
        pass

    def mercado_spot(self, sim):
        return dict(min_cant=0.0, min_usdt=1.0)

    def contrato(self, sim):
        return self.contratos.get(sim, dict(tam=1.0, minimo=1.0, sym=sim))

    def precios_spot(self, sims):
        out = {}
        for s in sims:
            try:
                p = self.precio(s, "spot")
                if p:
                    out[s] = float(p)
            except Exception:
                pass
        return out

    def precio_futuro(self, sim):
        return float(self.precio(sim, "fut"))

    def usdt_a_trading(self):
        pass

    def saldos_spot(self):
        return {k: v for k, v in self.st["spot"].items() if v > 1e-12}

    def usdt_spot_libre(self):
        return self.st["spot"].get("USDT", 0.0)

    def comprar_spot(self, sim, usdt):
        usdt = min(usdt, self.usdt_spot_libre())
        if usdt < 1:
            return None
        px = self.precio(sim, "spot") * (1 + self.d)
        com = usdt * self.cs
        q = (usdt - com) / px
        self.st["spot"]["USDT"] -= usdt
        self.st["spot"][sim] = self.st["spot"].get(sim, 0.0) + q
        self._g()
        return dict(cantidad=q, precio=px, usdt=usdt, comision=com, orden=uuid.uuid4().hex[:12])

    def vender_spot(self, sim, cantidad):
        q = min(cantidad, self.st["spot"].get(sim, 0.0))
        if q <= 0:
            return None
        px = self.precio(sim, "spot") * (1 - self.d)
        bruto = q * px; com = bruto * self.cs
        self.st["spot"][sim] -= q
        if self.st["spot"][sim] <= 1e-12:
            self.st["spot"].pop(sim)
        self.st["spot"]["USDT"] = self.st["spot"].get("USDT", 0.0) + bruto - com
        self._g()
        return dict(cantidad=q, precio=px, usdt=bruto - com, comision=com, orden=uuid.uuid4().hex[:12])

    def posiciones_futuros(self):
        return {s: p["c"] for s, p in self.st["fut"].items() if p["c"]}

    def _pnl(self):
        tot = 0.0
        for s, p in self.st["fut"].items():
            tot += p["c"] * self.contrato(s)["tam"] * (self.precio(s, "fut") - p["px"])
        return tot

    def patrimonio_futuros(self):
        return self.st["fut_caja"] + self._pnl()

    def libre_futuros(self):
        marg = sum(abs(p["c"]) * self.contrato(s)["tam"] * self.precio(s, "fut") for s, p in self.st["fut"].items()) / 3
        return self.patrimonio_futuros() - marg

    def _mover(self, sim, delta):
        tam = self.contrato(sim)["tam"]
        px = self.precio(sim, "fut") * (1 + self.d if delta > 0 else 1 - self.d)
        p = self.st["fut"].get(sim, dict(c=0.0, px=px))
        com = abs(delta) * tam * px * self.cf
        self.st["fut_caja"] -= com
        nuevo = p["c"] + delta
        if p["c"] == 0 or (p["c"] > 0) == (delta > 0):
            p["px"] = (p["c"] * p["px"] + delta * px) / nuevo
        else:
            self.st["fut_caja"] += (-delta) * tam * (px - p["px"])
        p["c"] = nuevo
        if abs(nuevo) < 1e-12:
            self.st["fut"].pop(sim, None)
        else:
            self.st["fut"][sim] = p
        self._g()
        return dict(contratos=abs(delta), precio=px, nocional=abs(delta) * tam * px, comision=com, orden=uuid.uuid4().hex[:12])

    def vender_contratos(self, sim, n):
        n = int(n)
        return self._mover(sim, -n) if n >= 1 else None

    def comprar_contratos(self, sim, n):
        actual = self.st["fut"].get(sim, {}).get("c", 0.0)
        n = min(int(n), int(abs(actual))) if actual < 0 else 0
        return self._mover(sim, n) if n >= 1 else None

    def margen_cruzado(self, sim):
        pass

    def aplicar_funding(self, sim, tasa):
        p = self.st["fut"].get(sim)
        if not p:
            return 0.0
        pago = p["c"] * self.contrato(sim)["tam"] * self.precio(sim, "fut") * tasa
        self.st["fut_caja"] -= pago
        self._g()
        return pago

    def transferir(self, monto, hacia):
        if hacia == "futuros":
            monto = min(monto, self.st["spot"].get("USDT", 0.0))
            self.st["spot"]["USDT"] -= monto; self.st["fut_caja"] += monto
        else:
            monto = min(monto, max(self.libre_futuros(), 0.0))
            self.st["fut_caja"] -= monto; self.st["spot"]["USDT"] = self.st["spot"].get("USDT", 0.0) + monto
        self._g()
        return monto
