"""Acceso al exchange. KuCoin futuros USDT-M vía ccxt (modo real) y un exchange simulado (modo papel).

Símbolos internos: el ticker base ("BTC", "SOL"…). En ccxt: "BTC/USDT:USDT" (KuCoin lo llama XBTUSDTM).
Cantidades siempre en contratos; nocional = contratos × tamaño de contrato × precio.
"""
import time
import uuid

import pandas as pd

TF_MS = {"4h": 4 * 3600_000, "1d": 86400_000}


def ccxt_sym(base):
    return f"{base}/USDT:USDT"


class Publico:
    """Datos públicos de KuCoin futuros (velas, mercados, precios). No necesita claves."""

    def __init__(self, ex=None):
        if ex is None:
            import ccxt
            ex = ccxt.kucoinfutures({"enableRateLimit": True})
        self.ex = ex
        self._mercados = None

    def mercados(self, refrescar=False):
        if self._mercados is None or refrescar:
            m = self.ex.load_markets(refrescar)
            out = {}
            for s, d in m.items():
                if d.get("swap") and d.get("linear") and d.get("quote") == "USDT" and d.get("settle") == "USDT" and d.get("active", True):
                    out[d["base"]] = dict(tam=float(d.get("contractSize") or 1), minimo=float((d.get("limits", {}).get("amount", {}) or {}).get("min") or 1),
                                          id=d["id"])
            self._mercados = out
        return self._mercados

    def velas(self, base, tf, desde_ms, hasta_ms=None):
        """Velas cerradas desde desde_ms (incl.). Pagina de a 200."""
        sym = ccxt_sym(base); paso = TF_MS[tf]; out = []
        ahora = int(time.time() * 1000) if hasta_ms is None else hasta_ms
        t = desde_ms
        while t <= ahora - paso:        # la vela que abre en ahora-paso ya cerró
            lote = self.ex.fetch_ohlcv(sym, tf, since=t, limit=200)
            if not lote:
                break
            out += lote
            nuevo = lote[-1][0] + paso
            if nuevo <= t:
                break
            t = nuevo
        if not out:
            return pd.DataFrame(columns=["o", "h", "l", "c", "v"])
        d = pd.DataFrame(out, columns=["ts", "o", "h", "l", "c", "v"]).drop_duplicates("ts")
        d = d[d.ts + paso <= ahora]           # sólo velas cerradas
        d.index = pd.to_datetime(d.pop("ts"), unit="ms")
        return d.astype(float)

    def precios(self, bases):
        quiero = {ccxt_sym(b) for b in bases}
        try:
            t = self.ex.fetch_tickers()
        except Exception:
            t = {}
            for s in quiero:
                try:
                    t[s] = self.ex.fetch_ticker(s)
                except Exception:
                    pass
        return {s.split("/")[0]: float(v.get("last") or v.get("close")) for s, v in t.items()
                if s in quiero and (v.get("last") or v.get("close"))}

    def funding(self, base):
        try:
            return float(self.ex.fetch_funding_rate(ccxt_sym(base)).get("fundingRate") or 0)
        except Exception:
            return None


# ---------------------------------------------------------------------------------------------------------------------
# Interfaz común de la cuenta principal (real y papel):
#   enviar_orden(base, lado, contratos, reduce_only, client_oid, precio_ref) -> dict(estado, llenado, precio, comision, orden_id)
#        estado: cerrada (llenó todo) | cancelada (llenó una parte o nada y se canceló el resto) | rechazada | abierta
#   estado_orden(base, client_oid) -> el mismo dict, o None si el exchange no la conoce
#   enviar_stop(base, lado, contratos, precio, client_oid) -> id   (reduce-only; lanza excepción si se rechaza)
#   estado_stop(base, id) -> dict(estado=abierta|ejecutada|cancelada|desconocida, llenado, precio)
#   cancelar_stop(base, id) -> True si quedó cancelado (o ya no estaba abierto), False si no se pudo
#   posiciones() -> {base: contratos con signo};  patrimonio() -> USDT
# ---------------------------------------------------------------------------------------------------------------------
ESTADOS_CCXT = {"closed": "cerrada", "canceled": "cancelada", "cancelled": "cancelada", "expired": "cancelada",
                "rejected": "rechazada", "open": "abierta"}


class KucoinReal:
    """Cuenta de futuros USDT-M real. Aperturas: límite IOC con tope de deslizamiento. Reducciones: a mercado,
    siempre reduce-only. Stops: reduce-only en el exchange."""

    modo = "real"
    ESPERA = 1.0          # segundos entre consultas del estado de una orden
    INTENTOS = 10

    def __init__(self, credenciales, publico, apalancamiento=3, deslizamiento_max=0.005, ex=None):
        if ex is None:
            import ccxt
            ex = ccxt.kucoinfutures({**credenciales, "enableRateLimit": True})
        self.ex = ex
        self.pub = publico
        self.lev = apalancamiento
        self.desl = deslizamiento_max

    def patrimonio(self):
        b = self.ex.fetch_balance({"currency": "USDT"})
        try:
            return float(b["info"]["data"]["accountEquity"])
        except Exception:
            return float(b["total"]["USDT"])

    def posiciones(self):
        out = {}
        for p in self.ex.fetch_positions():
            c = float(p.get("contracts") or 0)
            if c:
                out[p["symbol"].split("/")[0]] = c if p.get("side") == "long" else -c
        return out

    # ------------------------------------------------------------ órdenes
    @staticmethod
    def _resultado(f, oid):
        llenado = float(f.get("filled") or 0)
        estado = ESTADOS_CCXT.get(f.get("status"), "abierta" if f.get("status") is None else "desconocida")
        if estado == "cerrada" and f.get("amount") and llenado < float(f["amount"]) - 1e-9:
            estado = "cancelada"
        precio = f.get("average") or (f.get("price") if llenado else None)
        return dict(estado=estado, llenado=llenado, precio=float(precio) if precio else None,
                    comision=float((f.get("fee") or {}).get("cost") or 0), orden_id=f.get("id") or oid)

    def _esperar(self, oid, sym):
        f = {}
        for _ in range(self.INTENTOS):
            f = self.ex.fetch_order(oid, sym)
            if f.get("status") not in ("open", None):
                return f
            time.sleep(self.ESPERA)
        return f

    def enviar_orden(self, base, lado, contratos, reduce_only=False, client_oid=None, precio_ref=None):
        sym = ccxt_sym(base)
        params = {"clientOid": client_oid or uuid.uuid4().hex, "marginMode": "cross", "leverage": self.lev}
        if reduce_only:
            params["reduceOnly"] = True
        if precio_ref and self.desl and not reduce_only:
            px = precio_ref * (1 + self.desl if lado == "buy" else 1 - self.desl)
            try:
                px = float(self.ex.price_to_precision(sym, px))
            except Exception:
                pass
            params["timeInForce"] = "IOC"
            o = self.ex.create_order(sym, "limit", lado, contratos, px, params)
        else:
            o = self.ex.create_order(sym, "market", lado, contratos, None, params)
        oid = o["id"]
        f = self._esperar(oid, sym)
        if f.get("status") in ("open", None):          # quedó una parte sin llenar: se cancela el resto
            try:
                self.ex.cancel_order(oid, sym)
            except Exception:
                pass
            f = self._esperar(oid, sym)
        return self._resultado(f, oid)

    def estado_orden(self, base, client_oid):
        try:
            f = self.ex.fetch_order(None, ccxt_sym(base), {"clientOid": client_oid})
        except Exception:
            return None
        return self._resultado(f, f.get("id")) if f else None

    def enviar_stop(self, base, lado, contratos, precio, client_oid=None):
        params = {"reduceOnly": True, "triggerPrice": precio, "clientOid": client_oid or uuid.uuid4().hex,
                  "leverage": self.lev, "marginMode": "cross"}
        o = self.ex.create_order(ccxt_sym(base), "market", lado, contratos, None, params)
        return o["id"]

    def estado_stop(self, base, orden_id):
        sym = ccxt_sym(base)
        try:
            if any(o["id"] == orden_id for o in self.ex.fetch_open_orders(sym, params={"trigger": True})):
                return dict(estado="abierta", llenado=0.0, precio=None)
        except Exception:
            pass
        try:
            f = self.ex.fetch_order(orden_id, sym)
        except Exception:
            return dict(estado="desconocida", llenado=0.0, precio=None)
        llen = float(f.get("filled") or 0)
        if llen > 0:
            px = f.get("average") or f.get("price")
            return dict(estado="ejecutada", llenado=llen, precio=float(px) if px else None)
        if f.get("status") in ("canceled", "cancelled", "expired", "rejected", "closed"):
            return dict(estado="cancelada", llenado=0.0, precio=None)
        if f.get("status") == "open":
            return dict(estado="abierta", llenado=0.0, precio=None)
        return dict(estado="desconocida", llenado=0.0, precio=None)

    def cancelar_stop(self, base, orden_id):
        try:
            self.ex.cancel_order(orden_id, ccxt_sym(base), {"trigger": True})
            return True
        except Exception:
            return self.estado_stop(base, orden_id)["estado"] != "abierta"


class Papel:
    """Exchange simulado con la misma interfaz que KucoinReal: llena a mercado al último precio (± deslizamiento),
    cobra comisión, respeta reduce-only y dispara stops con el máximo/mínimo de cada vela que le pasa el motor."""

    modo = "papel"

    def __init__(self, base_datos, mercados, capital=3000.0, comision=0.0006, desliz=0.0005, clave="papel"):
        self.db = base_datos; self.merc = mercados; self.com = comision; self.desliz = desliz; self.clave = clave
        st = self.db.get(clave)
        if st is None:
            st = dict(caja=capital, pos={}, stops={}, ultimo={}, ordenes={})
            self.db.set(clave, st)
        st.setdefault("ordenes", {})
        self.st = st

    def _guardar(self):
        self.db.set(self.clave, self.st)

    def fijar_precios(self, precios):
        self.st["ultimo"].update({k: float(v) for k, v in precios.items()})

    def patrimonio(self):
        e = self.st["caja"]
        for b, p in self.st["pos"].items():
            px = self.st["ultimo"].get(b, p["px"])
            e += p["c"] * self.merc[b]["tam"] * (px - p["px"])
        return e

    def posiciones(self):
        return {b: p["c"] for b, p in self.st["pos"].items() if p["c"]}

    def _llenar(self, base, delta, precio):
        tam = self.merc[base]["tam"]
        p = self.st["pos"].get(base, dict(c=0.0, px=precio))
        com = abs(delta) * tam * precio * self.com
        self.st["caja"] -= com
        nuevo = p["c"] + delta
        if p["c"] == 0 or (p["c"] > 0) == (delta > 0):          # abre o agranda
            p["px"] = (p["c"] * p["px"] + delta * precio) / nuevo if nuevo else precio
        else:                                                    # reduce o cruza
            cerrado = -delta if abs(delta) <= abs(p["c"]) else p["c"]
            self.st["caja"] += cerrado * tam * (precio - p["px"])
            if abs(delta) > abs(p["c"]):
                p["px"] = precio
        p["c"] = nuevo
        if abs(p["c"]) < 1e-12:
            self.st["pos"].pop(base, None)
        else:
            self.st["pos"][base] = p
        return com

    def _acotar(self, base, lado, contratos, reduce_only):
        """Reduce-only: sólo hasta cerrar la posición; sin posición (o del mismo lado) no llena nada."""
        if not reduce_only:
            return contratos
        actual = self.st["pos"].get(base, {}).get("c", 0.0)
        if actual == 0 or (actual > 0) == (lado == "buy"):
            return 0.0
        return min(contratos, abs(actual))

    def enviar_orden(self, base, lado, contratos, reduce_only=False, client_oid=None, precio_ref=None):
        oid = client_oid or uuid.uuid4().hex
        q = self._acotar(base, lado, float(contratos), reduce_only)
        px = self.st["ultimo"][base] * (1 + self.desliz if lado == "buy" else 1 - self.desliz)
        com = self._llenar(base, q if lado == "buy" else -q, px) if q > 0 else 0.0
        r = dict(estado="cerrada" if q >= contratos else "cancelada" if q > 0 else "rechazada", llenado=q,
                 precio=px if q > 0 else None, comision=com, orden_id=oid)
        self.st["ordenes"][oid] = r
        if len(self.st["ordenes"]) > 300:
            for k in list(self.st["ordenes"])[:100]:
                self.st["ordenes"].pop(k)
        self._guardar()
        return r

    def estado_orden(self, base, client_oid):
        return self.st["ordenes"].get(client_oid)

    def enviar_stop(self, base, lado, contratos, precio, client_oid=None):
        oid = client_oid or uuid.uuid4().hex
        self.st["stops"][oid] = dict(base=base, lado=lado, c=contratos, px=precio, estado="abierta", llenado=0.0, precio=None)
        self._guardar()
        return oid

    def estado_stop(self, base, orden_id):
        s = self.st["stops"].get(orden_id)
        if not s:
            return dict(estado="desconocida", llenado=0.0, precio=None)
        return dict(estado=s.get("estado", "abierta"), llenado=s.get("llenado", 0.0), precio=s.get("precio"))

    def cancelar_stop(self, base, orden_id):
        s = self.st["stops"].get(orden_id)
        if s and s.get("estado", "abierta") == "abierta":
            s["estado"] = "cancelada"
            self._guardar()
        return True

    def revisar_stops(self, velas_por_base):
        """velas_por_base: base -> (o, h, l) de la vela recién cerrada. Llena al stop o a la apertura si la saltó."""
        for oid, s in list(self.st["stops"].items()):
            if s.get("estado", "abierta") != "abierta":
                continue
            v = velas_por_base.get(s["base"])
            if v is None:
                continue
            o, h, l = v[:3]
            if s["lado"] == "sell" and l <= s["px"]:
                px = min(o, s["px"])
            elif s["lado"] == "buy" and h >= s["px"]:
                px = max(o, s["px"])
            else:
                continue
            q = self._acotar(s["base"], s["lado"], s["c"], True)
            if q > 0:
                self._llenar(s["base"], q if s["lado"] == "buy" else -q, px)
                s.update(estado="ejecutada", llenado=q, precio=px)
            else:
                s.update(estado="cancelada")
        hechos = [k for k, s in self.st["stops"].items() if s.get("estado", "abierta") != "abierta"]
        for k in hechos[:-200]:
            self.st["stops"].pop(k)
        self._guardar()
