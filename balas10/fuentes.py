"""Velas de BTCUSDT de Binance spot (4 h y diarias), sólo cerradas. Se usan para las señales; se opera en KuCoin."""
import pandas as pd

from sistema3.fuentes import SPOT, _get, _velas


class BinanceBTC:
    def btc_4h(self, n=1000):
        return _velas(_get(f"{SPOT}/api/v3/klines", dict(symbol="BTCUSDT", interval="4h", limit=n)))["c"]

    def btc_1d(self, n=1000):
        return _velas(_get(f"{SPOT}/api/v3/klines", dict(symbol="BTCUSDT", interval="1d", limit=n)))["c"]


class SerieFija:
    """Fuente de pruebas: cierres de 4 h dados; el diario es el cierre de la vela de las 20:00 (cierra 00:00 UTC)."""
    def __init__(self, c4):
        self.c4 = c4

    def btc_4h(self, n=1000):
        return self.c4

    def btc_1d(self, n=1000):
        d = self.c4[(self.c4.index + pd.Timedelta(hours=4)).hour == 0]
        d.index = d.index.normalize()
        return d
