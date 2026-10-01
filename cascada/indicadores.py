"""Indicadores, iguales a los del backtest del proyecto."""
import numpy as np
import pandas as pd


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi_wilder(c, n=2):
    d = c.diff()
    au = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    ad = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + au / ad)


def tr(h, l, c):
    pc = c.shift()
    return np.maximum(h - l, np.maximum((h - pc).abs(), (l - pc).abs()))


def atr_wilder(h, l, c, n=14, min_periods=14):
    return tr(h, l, c).ewm(alpha=1 / n, adjust=False, min_periods=min_periods).mean()


def atr_media(h, l, c, n=14):
    return tr(h, l, c).rolling(n).mean()


def diario_desde_4h(d4):
    """Velas diarias UTC a partir de velas de 4h indexadas por apertura (o, h, l, c, v)."""
    g = d4.groupby(d4.index.floor("D"))
    d = pd.DataFrame({"o": g.o.first(), "h": g.h.max(), "l": g.l.min(), "c": g.c.last(), "v": g.v.sum(),
                      "n": g.c.count()})
    return d
