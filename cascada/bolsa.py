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


class KucoinReal:
    """Cuenta de futuros USDT-M real. Todas las órdenes son a mercado con tope de deslizamiento o stops reduce-only."""

    modo = "real"

    def __init__(self, credenciales, publico, apalancamiento=3):
        import ccxt
        self.ex = ccxt.kucoinfutures({**credenciales, "enableRateLimit": True})
        self.pub = publico
        self.lev = apalancamiento

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

    def orden_mercado(self, base, lado, contratos, reduce_only=False, client_oid=None):
        params = {"reduceOnly": reduce_only, "clientOid": client_oid or uuid.uuid4().hex, "leverage": self.lev,
                  "marginMode": "cross"}
        o = self.ex.create_order(ccxt_sym(base), "market", lado, contratos, None, params)
        oid = o["id"]
        for _ in range(10):
            f = self.ex.fetch_order(oid, ccxt_sym(base))
            if f.get("status") == "closed" or f.get("filled"):
                break
            time.sleep(1)
        precio = float(f.get("average") or f.get("price") or 0)
        com = float((f.get("fee") or {}).get("cost") or 0)
        return dict(id=oid, precio=precio, contratos=float(f.get("filled") or contratos), comision=com)

    def orden_stop(self, base, lado, contratos, precio_stop, client_oid=None):
        params = {"reduceOnly": True, "triggerPrice": precio_stop, "clientOid": client_oid or uuid.uuid4().hex,
                  "leverage": self.lev, "marginMode": "cross"}
        o = self.ex.create_order(ccxt_sym(base), "market", lado, contratos, None, params)
        return o["id"]

    def cancelar_stop(self, base, orden_id):
        try:
            self.ex.cancel_order(orden_id, ccxt_sym(base), {"trigger": True})
        except Exception:
            pass

    def stops_abiertos(self, bases):
        ids = set()
        for b in bases:
            for o in self.ex.fetch_open_orders(ccxt_sym(b), params={"trigger": True}):
                ids.add(o["id"])
        return ids

    def precio_stop(self, base, orden_id, desde_ms):
        """Precio al que se ejecutó un stop (None si no se sabe)."""
        try:
            o = self.ex.fetch_order(orden_id, ccxt_sym(base), {"trigger": True})
            px = o.get("average") or o.get("price")
            if px:
                return float(px)
        except Exception:
            pass
        try:
            tr = self.ex.fetch_my_trades(ccxt_sym(base), since=desde_ms)
            return float(tr[-1]["price"]) if tr else None
        except Exception:
            return None


class Papel:
    """Exchange simulado: llena a mercado al último precio (± deslizamiento), cobra comisión y
    dispara stops con el máximo/mínimo de cada vela que el motor le pasa."""

    modo = "papel"

    def __init__(self, base_datos, mercados, capital=3000.0, comision=0.0006, desliz=0.0005, clave="papel"):
        self.db = base_datos; self.merc = mercados; self.com = comision; self.desliz = desliz; self.clave = clave
        st = self.db.get(clave)
        if st is None:
            st = dict(caja=capital, pos={}, stops={}, ultimo={}, stops_hechos=[])
            self.db.set(clave, st)
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

    def _llenar(self, base, delta, precio, motivo=""):
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

    def orden_mercado(self, base, lado, contratos, reduce_only=False, client_oid=None):
        px = self.st["ultimo"][base] * (1 + self.desliz if lado == "buy" else 1 - self.desliz)
        delta = contratos if lado == "buy" else -contratos
        com = self._llenar(base, delta, px)
        self._guardar()
        return dict(id=client_oid or uuid.uuid4().hex, precio=px, contratos=contratos, comision=com)

    def orden_stop(self, base, lado, contratos, precio_stop, client_oid=None):
        oid = client_oid or uuid.uuid4().hex
        self.st["stops"][oid] = dict(base=base, lado=lado, c=contratos, px=precio_stop)
        self._guardar()
        return oid

    def cancelar_stop(self, base, orden_id):
        self.st["stops"].pop(orden_id, None); self._guardar()

    def stops_abiertos(self, bases=None):
        return set(self.st["stops"])

    def revisar_stops(self, velas_por_base):
        """velas_por_base: base -> (o, h, l) de la vela recién cerrada. Llena al stop o a la apertura si la saltó."""
        for oid, s in list(self.st["stops"].items()):
            v = velas_por_base.get(s["base"])
            if v is None:
                continue
            o, h, l = v
            if s["lado"] == "sell" and l <= s["px"]:
                px = min(o, s["px"])
            elif s["lado"] == "buy" and h >= s["px"]:
                px = max(o, s["px"])
            else:
                continue
            delta = s["c"] if s["lado"] == "buy" else -s["c"]
            self._llenar(s["base"], delta, px)
            self.st["stops"].pop(oid)
            self.st["stops_hechos"].append(dict(id=oid, base=s["base"], px=px))
        self._guardar()

    def precio_stop(self, base, orden_id, desde_ms):
        for s in reversed(self.st["stops_hechos"]):
            if s["id"] == orden_id:
                return s["px"]
        return None
