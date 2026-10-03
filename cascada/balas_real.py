"""Ejecución real de 30 balas en una subcuenta de KuCoin (futuro inverso XBTUSDM, 1 contrato = 1 USD, margen en BTC).

Flujo de cada acción:
  aportar margen  = comprar BTC con USDT en spot  →  pasar el BTC a la cuenta de futuros
  abrir/recargar  = aportar margen + comprar contratos XBTUSDM (margen cruzado)
  cerrar          = vender todos los contratos (reduce-only) y comprobar que la posición quedó en cero → pasar el BTC a
                    spot → venderlo por USDT

Ninguna orden se da por hecha sin un estado terminal del exchange: si sigue abierta se cancela el resto y se vuelve a
mirar; si aun así no hay estado terminal, se lanza `SinConfirmar` y 30 balas queda bloqueada hasta leer la subcuenta.
Las transferencias internas (spot ↔ futuros) prueban otra ruta sólo ante un rechazo explícito; ante un error de red se
mira el saldo de destino antes de hacer nada más. `estado()` devuelve lo que de verdad hay en la subcuenta: el modelo de
`balas.py` se iguala a eso después de cada acción. Probar primero con `cli balas-prueba`.
"""
import logging
import time

from .subcuenta import es_error_de_red

log = logging.getLogger("cascada.balas_real")
SPOT = "BTC/USDT"
INV = "BTC/USD:BTC"          # XBTUSDM en ccxt
TERMINALES = ("closed", "canceled", "cancelled", "rejected", "expired")
POLVO_BTC = 2e-5             # restos menores no se mueven ni se venden (mínimos de KuCoin)
MIN_USDT_SPOT = 0.1          # compra mínima en spot (minFunds de BTC-USDT)


class SinConfirmar(RuntimeError):
    """El exchange no dio un estado terminal (o se perdió la respuesta): hay que leer la subcuenta antes de seguir."""


class EjecutorRealBalas:
    INTENTOS = 15
    ESPERA = 1.0

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
                            marca=p.get("markPrice"), liquidacion=p.get("liquidationPrice"), modo_margen=p.get("marginMode"),
                            margen=p.get("collateral") or p.get("initialMargin"), pnl=p.get("unrealizedPnl"),
                            apalancamiento=p.get("leverage"))
        return None

    def estado(self):
        """Lo que hay en la subcuenta: USDT y BTC en spot (main + trade), BTC realizado en futuros (sin el resultado
        abierto) y la posición de XBTUSDM. Lanza excepción si alguna lectura falla (no se decide con datos a medias)."""
        usdt = btc_spot = 0.0
        for tipo in ("main", "trade"):
            b = self.spot.fetch_balance({"type": tipo})
            usdt += float(b["total"].get("USDT") or 0); btc_spot += float(b["total"].get("BTC") or 0)
        p = self.posicion()
        b = self.fut.fetch_balance({"code": "BTC"})
        d = (b.get("info") or {}).get("data") or {}
        if d.get("accountEquity") is not None:
            margen = float(d["accountEquity"]) - float(d.get("unrealisedPNL") or 0)
        else:
            margen = float(b["total"].get("BTC") or 0)
            if p and p.get("entrada") and p.get("marca"):
                margen -= p["contratos"] * (1 / float(p["entrada"]) - 1 / float(p["marca"]))
        return dict(usdt=usdt, btc_spot=btc_spot, margen_btc=margen, contratos=float(p["contratos"]) if p else 0.0,
                    entrada=float(p["entrada"]) if p and p.get("entrada") else None)

    def btc_futuros_libre(self):
        b = self.fut.fetch_balance({"code": "BTC"})
        return float(b["free"].get("BTC") or 0)

    def _btc_spot_trade(self):
        b = self.spot.fetch_balance({"type": "trade"})
        return float(b["free"].get("BTC") or 0)

    # ------------------------------------------------------------ órdenes con estado terminal
    def _esperar(self, inst, oid, sym):
        """Hasta un estado terminal. Si la orden sigue abierta, cancela el resto y vuelve a mirar; si no hay estado
        terminal, `SinConfirmar`."""
        f = {}
        for vuelta in range(2):
            for _ in range(self.INTENTOS):
                try:
                    f = inst.fetch_order(oid, sym)
                    if f.get("status") in TERMINALES:
                        return f
                except Exception as e:
                    log.warning("consulta de la orden %s: %s", oid, e)
                time.sleep(self.ESPERA)
            if vuelta == 0:
                try:
                    inst.cancel_order(oid, sym)
                except Exception as e:
                    log.warning("cancelar %s: %s", oid, e)
        raise SinConfirmar(f"orden {oid} en {sym} sin estado terminal (último: {f.get('status')}, llenado {f.get('filled')})")

    def comprar_btc(self, usd):
        """Compra BTC por `usd` USDT a mercado. Devuelve (BTC recibidos netos de comisión, orden)."""
        if usd < MIN_USDT_SPOT:
            return 0.0, None
        self.usdt_a_trading()
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
        if q < POLVO_BTC:
            return None
        o = self.spot.create_order(SPOT, "market", "sell", q)
        f = self._esperar(self.spot, o["id"], SPOT)
        log.info("venta spot %.8f BTC a %s", q, f.get("average"))
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
        log.info("XBTUSDM %s %d contratos: llenó %s a %s", lado, n, f.get("filled"), f.get("average"))
        return f

    # ------------------------------------------------------------ transferencias internas (spot ↔ futuros)
    def _transferir(self, monto, rutas, nombre, saldo_destino):
        """Prueba rutas en orden; sólo pasa a la siguiente ante un rechazo explícito. Ante un error de red mira el saldo
        de destino: si llegó, listo; si no se puede confirmar, `SinConfirmar` (no se manda otra)."""
        errores = []
        for inst, desde, hacia in rutas:
            antes = saldo_destino()
            try:
                r = inst.transfer("BTC", monto, desde, hacia)
                log.info("transferencia %s %.8f BTC por %s %s→%s: %s", nombre, monto, inst.id, desde, hacia, (r or {}).get("id"))
                return (inst, desde, hacia)
            except Exception as e:
                if not es_error_de_red(e):
                    errores.append(f"{getattr(inst, 'id', '?')} {desde}→{hacia}: {str(e)[:160]}")
                    continue
                for espera in (1, 2, 4, 8):
                    time.sleep(espera)
                    try:
                        if saldo_destino() - antes >= monto * 0.999:
                            log.warning("transferencia %s sin respuesta pero confirmada por saldo", nombre)
                            return (inst, desde, hacia)
                    except Exception:
                        pass
                raise SinConfirmar(f"transferencia {nombre} de {monto:.8f} BTC sin respuesta y sin confirmar por saldo: {e}")
        raise RuntimeError(f"No pude transferir BTC ({nombre}). Intentos:\n  " + "\n  ".join(errores))

    def a_futuros(self, monto):
        rutas = [self.ruta_ida] if self.ruta_ida else [
            (self.spot, "trade", "future"), (self.spot, "trade", "contract"), (self.spot, "hf", "future"),
            (self.spot, "main", "future"), (self.fut, "spot", "future")]
        self.ruta_ida = self._transferir(monto, rutas, "spot→futuros", self.btc_futuros_libre)

    def a_spot(self, monto):
        rutas = [self.ruta_vuelta] if self.ruta_vuelta else [
            (self.fut, "future", "trade"), (self.fut, "future", "spot"), (self.spot, "future", "trade"),
            (self.fut, "future", "main"), (self.spot, "contract", "trade")]
        self.ruta_vuelta = self._transferir(monto, rutas, "futuros→spot", self._btc_spot_trade)

    def usdt_a_trading(self):
        """Si el USDT quedó en la cuenta principal (main), lo pasa a la de trading."""
        b = self.spot.fetch_balance({"type": "main"})
        u = float(b["free"].get("USDT") or 0)
        if u > 0.01:
            self.spot.transfer("USDT", u, "main", "trade")
            log.info("USDT main→trade %.2f", u)
        return u

    def a_futuros_todo(self):
        """Completa un aporte de margen interrumpido: pasa a futuros todo el BTC que quedó en spot."""
        btc = self._btc_spot_trade()
        if btc > POLVO_BTC:
            self.a_futuros(round(btc * 0.9999, 8))
        return btc

    # ------------------------------------------------------------ interfaz que usa Balas
    def aportar_margen(self, usd, px=None):
        btc, _ = self.comprar_btc(usd)
        if btc > POLVO_BTC:
            self.a_futuros(round(btc * 0.9999, 8))
        return dict(btc=btc)

    def abrir(self, usd_margen, nocional_usd, px=None):
        m = self.aportar_margen(usd_margen)
        f = self.contratos(nocional_usd, "buy")
        return dict(m, llenado=(f or {}).get("filled"), precio=(f or {}).get("average"))

    def recargar(self, usd_margen, nocional_usd, px=None):
        m = self.aportar_margen(usd_margen)
        f = self.contratos(nocional_usd, "buy") if int(nocional_usd) >= 1 else None
        return dict(m, llenado=(f or {}).get("filled"), precio=(f or {}).get("average"))

    def cerrar(self, px=None):
        """Vende todos los contratos y comprueba que la posición quedó en cero (dos intentos); después pasa el BTC a spot y
        lo vende. Si la posición no queda plana, `SinConfirmar`."""
        out = {}
        for intento in range(2):
            p = self.posicion()
            if not p or p["contratos"] <= 0:
                break
            f = self.contratos(p["contratos"], "sell", reduce=True)
            out[f"cierre_{intento}"] = dict(llenado=(f or {}).get("filled"), precio=(f or {}).get("average"))
            time.sleep(2)
        p = self.posicion()
        if p and p["contratos"] > 0:
            raise SinConfirmar(f"la posición de XBTUSDM no quedó plana ({p['contratos']:g} contratos)")
        libre = self.btc_futuros_libre()
        if libre > POLVO_BTC:
            self.a_spot(round(libre * 0.9999, 8))
            time.sleep(2)
        btc = self._btc_spot_trade()
        if btc > POLVO_BTC:
            f = self.vender_btc(btc)
            out["venta_btc"] = dict(llenado=(f or {}).get("filled"), precio=(f or {}).get("average"))
        return out
