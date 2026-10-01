"""Datos: velas de 4h en la base local (incrementales) y universo top 50 semanal de CoinMarketCap."""
import json
import time
import urllib.request

import pandas as pd

from .indicadores import diario_desde_4h

HIST_DIAS = 420         # alcanza para SMA200 diaria, ROC90 y EMA300 de 4h
EXCLUIR_TAGS = {"stablecoin", "wrapped-tokens", "tokenized-gold", "asset-backed-stablecoin", "fiat-stablecoin"}
EXCLUIR_SIMBOLOS = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD", "USDS", "WBTC", "WETH", "STETH", "WSTETH",
                    "WEETH", "WBETH", "CBBTC", "USD1", "BUIDL", "USDTB", "XAUT", "PAXG", "BSC-USD", "SUSDE", "USDF"}


def top50_cmc(api_key, n=50):
    """Top n de CMC por capitalización, sin stables ni wrapped. Devuelve lista de (puesto, símbolo, vol24)."""
    req = urllib.request.Request(
        "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest?limit=120&convert=USD",
        headers={"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"})
    data = json.load(urllib.request.urlopen(req, timeout=30))["data"]
    out = []
    for d in data:
        tags = set(d.get("tags") or [])
        if d["symbol"].upper() in EXCLUIR_SIMBOLOS or tags & EXCLUIR_TAGS:
            continue
        out.append((len(out) + 1, d["symbol"].upper(), float(d["quote"]["USD"].get("volume_24h") or 0)))
        if len(out) == n:
            break
    return out


class Datos:
    def __init__(self, db, publico):
        self.db = db; self.pub = publico
        self._mem = {}          # base -> velas 4h completas en memoria (se invalida al actualizar)

    # ---------- universo ----------
    def actualizar_universo(self, api_key, ahora=None):
        """Guarda el top 50 del domingo. Si no hay ninguno guardado, toma el actual."""
        ahora = pd.Timestamp(ahora or pd.Timestamp.now("UTC").tz_localize(None))
        domingo = (ahora.normalize() - pd.Timedelta(days=(ahora.dayofweek + 1) % 7)).strftime("%Y-%m-%d")
        if self.db.filas("SELECT 1 FROM universo WHERE fecha=? LIMIT 1", (domingo,)):
            return False
        if not api_key:
            raise RuntimeError("Falta CMC_API_KEY para bajar el top 50")
        lista = top50_cmc(api_key)
        for p, s, v in lista:
            self.db.ejec("INSERT OR REPLACE INTO universo VALUES (?,?,?,?)", (domingo, s, p, v))
        return True

    def universo(self, t):
        """Top 50 vigente en t: el del último domingo anterior (vale desde el lunes 00:00)."""
        t = pd.Timestamp(t)
        r = self.db.filas("SELECT MAX(fecha) f FROM universo WHERE fecha < ?", (t.strftime("%Y-%m-%d"),))
        if not r or not r[0]["f"]:
            r = self.db.filas("SELECT MIN(fecha) f FROM universo")
        if not r or not r[0]["f"]:
            return []
        return [x["simbolo"] for x in self.db.filas("SELECT simbolo FROM universo WHERE fecha=? ORDER BY puesto", (r[0]["f"],))]

    # ---------- velas ----------
    def actualizar(self, bases, hasta_ms=None):
        """Trae velas 4h nuevas de cada base. Devuelve las bases con error."""
        errores = []
        desde_def = int((time.time() - HIST_DIAS * 86400) * 1000)
        for b in bases:
            try:
                u = self.db.ultimo_ts(b, "4h")
                desde = desde_def if u is None else u + 4 * 3600_000
                d = self.pub.velas(b, "4h", desde, hasta_ms)
                self.db.guardar_velas(b, "4h", d)
                self._mem.pop(b, None)
            except Exception as e:  # una moneda con problemas no frena al resto
                errores.append((b, str(e)[:200]))
        return errores

    def v4(self, base, hasta=None, dias=HIST_DIAS):
        """Velas 4h cerradas (cierre <= hasta; hasta = momento de decisión), de los últimos `dias` días."""
        d = self._mem.get(base)
        if d is None:
            d = self.db.velas(base, "4h", 0)
            self._mem[base] = d
        if hasta is None:
            return d
        h = pd.Timestamp(hasta)
        i1 = d.index.searchsorted(h - pd.Timedelta(hours=4), side="right")
        i0 = d.index.searchsorted(h - pd.Timedelta(days=dias))
        return d.iloc[i0:i1]

    def v1d(self, base, hasta=None, dias=HIST_DIAS):
        d4 = self.v4(base, hasta, dias)
        d = diario_desde_4h(d4)
        return d[d.n == 6].drop(columns="n")   # sólo días completos
