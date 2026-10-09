"""Datos públicos para las señales: velas de Binance (spot diarias y perpetuos USDT-M de 4 h) y top 50 de CoinMarketCap.

Las señales se calculan con los mismos datos con que se probó el sistema (Binance); la ejecución va en KuCoin.
"""
import json
import logging
import time
import urllib.parse
import urllib.request

import pandas as pd

log = logging.getLogger("sistema3.fuentes")

SPOT = "https://api.binance.com"
PERP = "https://fapi.binance.com"
PREFIJOS = ("", "1000", "1000000", "1M")

EXCLUIR_TAGS = {"stablecoin", "wrapped-tokens", "tokenized-gold", "asset-backed-stablecoin", "fiat-stablecoin",
                "liquid-staking-derivatives", "tokenized-assets"}
EXCLUIR_SIMBOLOS = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD", "USDS", "USDP", "WBTC", "WETH", "STETH",
                    "WSTETH", "WEETH", "WBETH", "CBBTC", "USD1", "BUIDL", "USDTB", "XAUT", "PAXG", "BSC-USD", "SUSDE",
                    "USDF", "RLUSD", "USDD", "FRAX", "LBTC", "SOLVBTC", "JITOSOL", "RETH", "METH", "BNSOL", "EZETH",
                    "RSETH", "CLBTC", "BBSOL", "USDX", "USDG", "USD0", "OUSG", "USYC"}


def _get(url, params=None, headers=None, intentos=4, timeout=30):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    err = None
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": "sistema3"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:                         # 429/5xx/red: reintenta con espera creciente
            err = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET {url[:120]}: {err}")


def _velas(filas):
    if not filas:
        return pd.DataFrame(columns=["o", "h", "l", "c", "qv"])
    d = pd.DataFrame([[int(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[4]), float(f[7]), int(f[6])] for f in filas],
                     columns=["ts", "o", "h", "l", "c", "qv", "cierre"])
    ahora = int(time.time() * 1000)
    d = d[d.cierre < ahora].drop(columns="cierre")          # sólo velas cerradas
    d.index = pd.to_datetime(d.pop("ts"), unit="ms")
    return d


class Binance:
    def __init__(self):
        self._spot = None
        self._perp = None

    # ---------------------------------------------------------------- mercados
    def mercados_spot(self, refrescar=False):
        """base → símbolo de Binance spot contra USDT (sólo los que operan)."""
        if self._spot is None or refrescar:
            info = _get(f"{SPOT}/api/v3/exchangeInfo", {"permissions": "SPOT"})
            self._spot = {s["baseAsset"]: s["symbol"] for s in info["symbols"]
                          if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"}
        return self._spot

    def mercados_perp(self, refrescar=False):
        """base (sin prefijo 1000) → dict(sym, desde_ms) de los perpetuos USDT-M que operan."""
        if self._perp is None or refrescar:
            info = _get(f"{PERP}/fapi/v1/exchangeInfo")
            crudo = {s["baseAsset"]: dict(sym=s["symbol"], desde_ms=int(s.get("onboardDate") or 0))
                     for s in info["symbols"]
                     if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"}
            out = dict(crudo)                       # el contrato sin prefijo tiene prioridad
            for base, d in crudo.items():
                for p in PREFIJOS[1:]:
                    if base.startswith(p) and len(base) > len(p) and not base[len(p)].isdigit():
                        out.setdefault(base[len(p):], d)
                        break
            self._perp = out
        return self._perp

    # ---------------------------------------------------------------- velas
    def diarias(self, base, n=400):
        sym = self.mercados_spot().get(base)
        if not sym:
            return None
        return _velas(_get(f"{SPOT}/api/v3/klines", dict(symbol=sym, interval="1d", limit=min(n, 1000))))

    def perp_4h(self, base, n=1000):
        m = self.mercados_perp().get(base)
        if not m:
            return None
        return _velas(_get(f"{PERP}/fapi/v1/klines", dict(symbol=m["sym"], interval="4h", limit=min(n, 1500))))

    def precio_spot(self, base):
        sym = self.mercados_spot().get(base)
        if not sym:
            return None
        return float(_get(f"{SPOT}/api/v3/ticker/price", dict(symbol=sym))["price"])


def top50_cmc(api_key, n=50):
    """Top n por capitalización de CoinMarketCap sin stablecoins, envueltos, staking líquido ni oro: [(puesto, símbolo)]."""
    if not api_key:
        raise RuntimeError("Falta CMC_API_KEY para bajar el top 50")
    data = _get("https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest",
                dict(limit=150, convert="USD"), headers={"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"})["data"]
    out = []
    for d in data:
        tags = set(d.get("tags") or [])
        s = d["symbol"].upper()
        if s in EXCLUIR_SIMBOLOS or tags & EXCLUIR_TAGS:
            continue
        out.append((len(out) + 1, s))
        if len(out) >= n:
            break
    return out


def anchas(dfs, campo):
    """{sim: DataFrame} → DataFrame ancho con la columna `campo` de cada uno."""
    return pd.DataFrame({s: d[campo] for s, d in dfs.items() if d is not None and len(d)}).sort_index()
