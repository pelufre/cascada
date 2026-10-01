"""Estrategias de la cuenta principal. Mismas reglas que el backtest del proyecto.

Cada estrategia, en su momento de decisión t (cierre de vela), devuelve la lista COMPLETA de lotes que quiere tener:
    {id, simbolo, lado (+1/-1), frac, stop_dist, stop, reescalable}
- frac: nocional como fracción de la asignación de la estrategia (al entrar, o siempre si es reescalable).
- stop_dist: distancia del stop inicial en precio; el ejecutor lo coloca a precio_entrada ∓ stop_dist.
- stop: nivel absoluto nuevo (para mover un stop ya puesto, p. ej. breakeven).
Un lote que estaba y ya no aparece se cierra en la apertura siguiente.
El estado de cada estrategia se guarda en la base; `abiertos` trae los lotes vivos del libro (con precio de entrada y stop).
"""
import numpy as np
import pandas as pd

from ..indicadores import atr_media, atr_wilder, ema, rsi_wilder


class Estrategia:
    nombre = ""
    tf = "4h"            # cada cuánto decide: "4h" o "1d" (00:00 UTC)

    def decide_en(self, t):
        return self.tf == "4h" or pd.Timestamp(t).hour == 0

    def paso(self, t, datos, st, abiertos, universo, mercados):
        raise NotImplementedError


# ------------------------------------------------------------------ RSI(2) 4h
class RSI2(Estrategia):
    tf = "4h"

    def __init__(self, base, thr=20, trend=200, salida=300):
        self.base = base; self.nombre = f"rsi2_{base.lower()}"; self.thr = thr; self.trend = trend; self.salida = salida

    def paso(self, t, datos, st, abiertos, universo, mercados):
        b = datos.v4(self.base, t)
        if len(b) < self.trend + 2:
            return []
        c = b.c
        R = rsi_wilder(c).iat[-1]; C = c.iat[-1]; ET = ema(c, self.trend).iat[-1]; EX = ema(c, self.salida).iat[-1]
        dentro = st.get("dentro", False); armado = st.get("armado", False)
        if dentro and st.get("id") in st.get("_cerrados", []):
            st["id"] = f"{self.nombre}_{pd.Timestamp(t):%Y%m%d%H}"
        if not dentro:
            if R <= self.thr and C > ET:
                dentro, armado = True, False
                st["id"] = f"{self.nombre}_{pd.Timestamp(t):%Y%m%d%H}"
        else:
            if not armado:
                if C > EX:
                    armado = True
            elif C < EX:
                dentro = False
        st.update(dentro=dentro, armado=armado)
        return [dict(id=st.get("id", self.nombre), simbolo=self.base, lado=1, frac=1.0, reescalable=True)] if dentro else []


# ------------------------------------------------------------------ Williams %R(2) diario
class WR2(Estrategia):
    tf = "1d"
    nombre = "wr2"

    def __init__(self, base="BTC", vt=0.40):
        self.base = base; self.vt = vt

    def paso(self, t, datos, st, abiertos, universo, mercados):
        d = datos.v1d(self.base, t)
        if len(d) < 202:
            return []
        hh, ll = d.h.rolling(2).max(), d.l.rolling(2).min()
        wr = (-100 * (hh - d.c) / (hh - ll)).iat[-1]
        sma = d.c.rolling(200).mean().iat[-1]
        up = d.h.diff(); dn = -d.l.diff()
        pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), d.index)
        mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), d.index)
        a = atr_wilder(d.h, d.l, d.c, 14, min_periods=1)
        pdi = (pdm.ewm(alpha=1 / 14, adjust=False).mean() / a).iat[-1]
        mdi = (mdm.ewm(alpha=1 / 14, adjust=False).mean() / a).iat[-1]
        vol = d.c.pct_change().rolling(20).std().iat[-1] * np.sqrt(365)
        cur = st.get("cur", 0.0)
        if cur > 0 and st.get("id") in st.get("_cerrados", []):
            st["id"] = f"wr2_{pd.Timestamp(t):%Y%m%d}"
        if cur == 0 and wr >= -20 and d.c.iat[-1] > sma and pdi > mdi:
            cur = float(min(1.0, self.vt / vol))
            st["id"] = f"wr2_{pd.Timestamp(t):%Y%m%d}"
        elif cur > 0 and wr <= -90:
            cur = 0.0
        st["cur"] = cur
        return [dict(id=st.get("id", "wr2"), simbolo=self.base, lado=1, frac=cur, reescalable=True)] if cur > 0 else []


# ------------------------------------------------------------------ Soldados BTC 1d
class Soldados(Estrategia):
    """Régimen Donchian 20/10 diario; un soldado por cada nuevo máximo de 20 días; stop k·ATR; breakeven a +1R.
    Decide entradas y fin de régimen a las 00:00; el paso a breakeven se revisa en cada vela de 4h."""
    tf = "4h"
    nombre = "sold_btc"

    def __init__(self, base="BTC", N=20, M=10, k=1.0, r=0.01, lev=1.0, cost=0.001):
        self.base = base; self.N = N; self.M = M; self.k = k; self.r = r; self.lev = lev; self.cost = cost

    def paso(self, t, datos, st, abiertos, universo, mercados):
        t = pd.Timestamp(t)
        sold = st.setdefault("sold", {})
        cerr = set(st.get("_cerrados", []))
        for i in list(sold):                      # los que cerró el stop ya no están en el libro
            if i in cerr or (i not in abiertos and sold[i].get("vivo")):
                sold.pop(i)
        # breakeven con la vela de 4h recién cerrada
        v4 = datos.v4(self.base, t, dias=3)
        if len(v4):
            hi = v4.h.iat[-1]
            for i, s in sold.items():
                ab = abiertos.get(i)
                if ab and not s.get("be") and ab.get("precio_entrada") and hi >= ab["precio_entrada"] + s["R"]:
                    s["be"] = True; s["stop"] = ab["precio_entrada"] * (1 + 2 * self.cost)
        if t.hour == 0:
            d = datos.v1d(self.base, t)
            if len(d) >= self.N + 15:
                hiN = d.h.iloc[-self.N - 1:-1].max(); loM = d.l.iloc[-self.M - 1:-1].min()
                c = d.c.iat[-1]; A = atr_media(d.h, d.l, d.c, 14).iat[-1]
                reg = st.get("reg", 0)
                if reg == 1 and c < loM:
                    reg = 0
                brk = c > hiN
                if brk:
                    reg = 1
                if reg == 0:
                    sold.clear()
                st["reg"] = reg
                if reg == 1 and brk and A > 0:
                    dist = self.k * A
                    frac = self.r * c / dist
                    usado = sum(s["frac"] for s in sold.values())
                    frac = min(frac, max(self.lev - usado, 0))
                    if frac > 1e-6:
                        sid = f"sold_{t:%Y%m%d}"
                        sold[sid] = dict(frac=frac, R=dist, be=False, stop=None, vivo=False)
        out = []
        for i, s in sold.items():
            if i in abiertos:
                s["vivo"] = True
            out.append(dict(id=i, simbolo=self.base, lado=1, frac=s["frac"], stop_dist=s["R"], stop=s.get("stop"),
                            reescalable=False))
        return out


# ------------------------------------------------------------------ Cortos Aberration top 50 (4h)
class CortosAberration(Estrategia):
    tf = "4h"
    nombre = "ab_cortos"

    def __init__(self, n=120, k=2.0, roc_dias=90, slots=3, caida_max=0.20):
        self.n = n; self.k = k; self.roc = roc_dias * 6; self.S = slots; self.caida = caida_max

    def paso(self, t, datos, st, abiertos, universo, mercados):
        pos = st.setdefault("pos", {})            # simbolo -> id del lote
        cerr = set(st.get("_cerrados", []))
        for s in list(pos):
            if pos[s] in cerr or (pos[s] not in abiertos and st.get("confirmado", {}).get(s)):
                pos.pop(s)
        btc = datos.v4("BTC", t, dias=100)
        if len(btc) <= self.roc:
            return self._salida(pos)
        reg = btc.c.iat[-1] / btc.c.iat[-1 - self.roc] - 1 < 0
        salen, cand = [], []
        for s in set(universo) | set(pos):
            if s == "BTC" or s not in mercados:
                continue
            d = datos.v4(s, t, dias=100)
            if len(d) <= self.roc:
                if s in pos and len(d) == 0:
                    salen.append(s)
                continue
            c = d.c
            sma = c.iloc[-self.n:].mean(); sd = c.iloc[-self.n:].std(ddof=0)
            ult = c.iat[-1]
            fresco = d.index[-1] + pd.Timedelta(hours=4) >= pd.Timestamp(t) - pd.Timedelta(hours=4 * 3)
            if s in pos:
                viejo = d.index[-1] + pd.Timedelta(hours=4) < pd.Timestamp(t) - pd.Timedelta(hours=4 * 36)
                if ult > sma or viejo:
                    salen.append(s)
                continue
            roc = ult / c.iat[-1 - self.roc] - 1
            vela_ok = (ult / d.o.iat[-1] - 1) > -self.caida
            if s in universo and fresco and reg and roc < 0 and ult < sma - self.k * sd and vela_ok:
                cand.append((roc, s))
        for s in salen:
            pos.pop(s, None)
        libres = self.S - len(pos)
        for roc, s in sorted(cand)[:max(libres, 0)]:
            pos[s] = f"ab_{s}_{pd.Timestamp(t):%Y%m%d%H}"
        st["confirmado"] = {s: True for s in pos if pos[s] in abiertos}
        return [dict(id=i, simbolo=s, lado=-1, frac=1 / self.S, reescalable=False) for s, i in pos.items()]

    def _salida(self, pos):
        return [dict(id=i, simbolo=s, lado=-1, frac=1 / self.S, reescalable=False) for s, i in pos.items()]


# ------------------------------------------------------------------ Momentum alts c40 (diario)
class MomentumC40(Estrategia):
    tf = "1d"
    nombre = "mom_alts"

    def __init__(self, slots=5, risk=0.02, k=3.0, hist_min=90):
        self.S = slots; self.risk = risk; self.k = k; self.hmin = hist_min

    def paso(self, t, datos, st, abiertos, universo, mercados):
        pos = st.setdefault("pos", {})            # simbolo -> dict(id, frac, dist)
        cerr = set(st.get("_cerrados", []))
        for s in list(pos):
            if pos[s]["id"] in cerr or (pos[s]["id"] not in abiertos and pos[s].get("vivo")):
                pos.pop(s)                        # salió por stop
        D = {}
        for s in set(universo) | {"BTC", "ETH"}:
            d = datos.v1d(s, t, dias=200)
            if len(d) >= 30:
                D[s] = d
        if "BTC" not in D or len(D["BTC"]) < 141:
            return self._lotes(pos, abiertos)
        b = D["BTC"].c
        btc_ok = b.iat[-1] > b.iloc[-140:].mean() and b.iat[-1] / b.iat[-85] - 1 > 0
        elig = [s for s in D if (s in universo or s in ("BTC", "ETH")) and len(D[s]) >= self.hmin]
        sobre = [s for s in elig if len(D[s]) >= 50 and D[s].c.iat[-1] > D[s].c.iloc[-50:].mean()]
        amp = len(sobre) / max(len(elig), 1)
        on = st.get("amp_on", False)
        if on and amp < 0.40:
            on = False
        elif not on and amp > 0.60:
            on = True
        st["amp_on"] = on
        if not (on and btc_ok):
            pos.clear()
            return []

        def score(c):
            return 0.5 * (c.iat[-1] / c.iat[-8] - 1) + 0.3 * (c.iat[-1] / c.iat[-15] - 1) + 0.2 * (c.iat[-1] / c.iat[-29] - 1)
        sb = score(b)
        cand = []
        for s in elig:
            if s == "BTC" or s not in mercados or s in pos or s not in universo:
                continue
            d = D[s]
            sc = score(d.c)
            A = atr_wilder(d.h, d.l, d.c, 14).iat[-1]
            if sc > 0 and sc > sb and A > 0 and np.isfinite(A):
                cand.append((sc / (A / d.c.iat[-1]), s, A, d.c.iat[-1]))
        libres = self.S - len(pos)
        for _, s, A, c in sorted(cand, reverse=True)[:max(libres, 0)]:
            frac = min(1 / self.S, self.risk / (self.k * A / c))
            pos[s] = dict(id=f"mom_{s}_{pd.Timestamp(t):%Y%m%d}", frac=float(frac), dist=float(self.k * A), vivo=False)
        return self._lotes(pos, abiertos)

    def _lotes(self, pos, abiertos):
        out = []
        for s, p in pos.items():
            if p["id"] in abiertos:
                p["vivo"] = True
            out.append(dict(id=p["id"], simbolo=s, lado=1, frac=p["frac"], stop_dist=p["dist"], reescalable=False))
        return out


def todas():
    return {e.nombre: e for e in (CortosAberration(), MomentumC40(), RSI2("BTC"), WR2(), Soldados(), RSI2("ETH"))}
