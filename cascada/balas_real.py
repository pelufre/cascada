"""Ejecución real de 30 balas en una subcuenta de KuCoin (futuro inverso XBTUSDM, 1 contrato = 1 USD, margen en BTC).

Flujo de cada acción:
  aportar margen  = comprar BTC con USDT en spot  →  pasar el BTC a la cuenta de futuros
  abrir/recargar  = aportar margen + comprar contratos XBTUSDM (margen cruzado)
  cerrar          = vender todos los contratos (reduce-only) → pasar el BTC a spot → venderlo por USDT

Las rutas de transferencia entre cuentas cambian según la cuenta (spot clásica u "HF") y la versión de ccxt, así que se
prueban varias y se recuerda la que funcionó. Todo queda registrado en el log. Probar primero con `cli balas-prueba`.
"""
import logging
import time

log = logging.getLogger("cascada.balas_real")
SPOT = "BTC/USDT"
INV = "BTC/USD:BTC"          # XBTUSDM en ccxt


class EjecutorRealBalas:
    def __init__(self, cred, apalancamiento=10, spot=None, fut=None):
        if spot is None or fut is None:
            import ccxt
            spot = ccxt.kucoin({**cred, "enableRateLimit": True})
            fut = ccxt.kucoinfutures({**cred, "enableRateLimit": True})
        self.spot, self.fut = spot, fut
        self.lev = apalancamiento        # sólo fija el margen inicial que reserva KuCoin; con margen cruzado manda el saldo
        self.ruta_ida = None             # (instancia, desde, hacia) que funcionó para spot → futuros
        self.ruta_vuelta = None
        self.spot.load_markets(); self.fut.load_markets()

    # ------------------------------------------------------------ lectura
    def saldos(self):
        out = {}
        for tipo in ("main", "trade"):
            try:
                b = self.spot.fetch_balance({"type": tipo})
                out[f"spot_{tipo}"] = {c: float(b["total"].get(c) or 0) for c in ("USDT", "BTC")}
            except Exception as e:
                out[f"spot_{tipo}"] = f"error: {e}"[:200]
        try:
            b = self.fut.fetch_balance({"code": "BTC"})
            out["futuros_BTC"] = dict(total=float(b["total"].get("BTC") or 0), libre=float(b["free"].get("BTC") or 0))
        except Exception as e:
            out["futuros_BTC"] = f"error: {e}"[:200]
        return out

    def posicion(self):
        for p in self.fut.fetch_positions([INV]):
            c = float(p.get("contracts") or 0)
            if c:
                return dict(contratos=c if p.get("side") == "long" else -c, entrada=p.get("entryPrice"),
                            liquidacion=p.get("liquidationPrice"), modo_margen=p.get("marginMode"),
                            margen=p.get("collateral") or p.get("initialMargin"), pnl=p.get("unrealizedPnl"),
                            apalancamiento=p.get("leverage"))
        return None

    def btc_futuros_libre(self):
        b = self.fut.fetch_balance({"code": "BTC"})
        return float(b["free"].get("BTC") or 0)

    # ------------------------------------------------------------ transferencias
    def _transferir(self, monto, rutas, nombre):
        errores = []
        for inst, desde, hacia in rutas:
            try:
                r = inst.transfer("BTC", monto, desde, hacia)
                log.info("transferencia %s %.8f BTC por %s %s→%s: %s", nombre, monto, inst.id, desde, hacia, r.get("id"))
                return (inst, desde, hacia)
            except Exception as e:
                errores.append(f"{inst.id} {desde}→{hacia}: {str(e)[:160]}")
        raise RuntimeError(f"No pude transferir BTC ({nombre}). Intentos:\n  " + "\n  ".join(errores))

    def a_futuros(self, monto):
        rutas = [self.ruta_ida] if self.ruta_ida else [
            (self.spot, "trade", "future"), (self.spot, "trade", "contract"), (self.spot, "hf", "future"),
            (self.spot, "main", "future"), (self.fut, "spot", "future")]
        self.ruta_ida = self._transferir(monto, rutas, "spot→futuros")

    def a_spot(self, monto):
        rutas = [self.ruta_vuelta] if self.ruta_vuelta else [
            (self.fut, "future", "trade"), (self.fut, "future", "spot"), (self.spot, "future", "trade"),
            (self.fut, "future", "main"), (self.spot, "contract", "trade")]
        self.ruta_vuelta = self._transferir(monto, rutas, "futuros→spot")

    def usdt_a_trading(self):
        """Si el USDT quedó en la cuenta principal (main), lo pasa a la de trading."""
        b = self.spot.fetch_balance({"type": "main"})
        u = float(b["free"].get("USDT") or 0)
        if u > 0.01:
            self.spot.transfer("USDT", u, "main", "trade")
            log.info("USDT main→trade %.2f", u)
        return u

    # ------------------------------------------------------------ órdenes
    def comprar_btc(self, usd):
        """Compra BTC por `usd` USDT a mercado. Devuelve los BTC recibidos (netos de comisión si la cobra en BTC)."""
        o = self.spot.create_market_buy_order_with_cost(SPOT, round(usd, 2))
        f = self._esperar(self.spot, o["id"], SPOT)
        btc = float(f.get("filled") or 0)
        fee = f.get("fee") or {}
        if fee.get("currency") == "BTC":
            btc -= float(fee.get("cost") or 0)
        log.info("compra spot %.2f USDT → %.8f BTC a %s", usd, btc, f.get("average"))
        return btc, f

    def vender_btc(self, btc):
        q = float(self.spot.amount_to_precision(SPOT, btc * 0.999))
        o = self.spot.create_order(SPOT, "market", "sell", q)
        f = self._esperar(self.spot, o["id"], SPOT)
        log.info("venta spot %.8f BTC a %s", q, f.get("average"))
        return f

    def _esperar(self, inst, oid, sym):
        f = {}
        for _ in range(15):
            try:
                f = inst.fetch_order(oid, sym)
                if f.get("status") == "closed":
                    return f
            except Exception:
                pass
            time.sleep(1)
        return f

    def margen_cruzado(self):
        try:
            self.fut.set_margin_mode("cross", INV)
            return "cross"
        except Exception as e:
            log.warning("set_margin_mode: %s", e)
            return f"no se pudo fijar: {str(e)[:150]}"

    def contratos(self, n, lado="buy", reduce=False):
        n = int(n)
        if n < 1:
            return None
        params = {"marginMode": "cross", "leverage": self.lev}
        if reduce:
            params["reduceOnly"] = True
        o = self.fut.create_order(INV, "market", lado, n, None, params)
        f = self._esperar(self.fut, o["id"], INV)
        log.info("XBTUSDM %s %d contratos a %s", lado, n, f.get("average"))
        return f

    # ------------------------------------------------------------ interfaz que usa Balas
    def aportar_margen(self, usd, px=None):
        btc, _ = self.comprar_btc(usd)
        self.a_futuros(round(btc * 0.9999, 8))
        return btc

    def abrir(self, usd_margen, nocional_usd, px=None):
        self.aportar_margen(usd_margen)
        return self.contratos(nocional_usd, "buy")

    def recargar(self, usd_margen, nocional_usd, px=None):
        self.aportar_margen(usd_margen)
        return self.contratos(nocional_usd, "buy") if int(nocional_usd) >= 1 else None

    def cerrar(self, px=None):
        p = self.posicion()
        if p and p["contratos"] > 0:
            self.contratos(p["contratos"], "sell", reduce=True)
        time.sleep(2)
        libre = self.btc_futuros_libre()
        if libre > 0:
            self.a_spot(round(libre * 0.9999, 8))
            time.sleep(2)
            b = self.spot.fetch_balance({"type": "trade"})
            btc = float(b["free"].get("BTC") or 0)
            if btc > 0:
                self.vender_btc(btc)
