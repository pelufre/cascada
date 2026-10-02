"""Cartera por lotes (enmienda E10): reconstruye la cartera Cascada a partir de las corridas de cada estrategia sola,
lote por lote, con las mismas reglas del asignador del motor. Evalúa muchos vectores de pesos a la vez (numpy).

Por qué: la cartera rápida de E2 escalaba cada estrategia con su exposición de la corrida sola. En el motor, los lotes
fijos (ab_cortos, mom_alts, sold_btc) se dimensionan al abrir (peso × g × patrimonio de ese momento) y después no se
achican, y 30 balas recibe su capital al empezar cada campaña y tiene su nocional reservado antes que nadie. Con pesos
chicos un ganador crece contra el patrimonio total mucho más que en la corrida sola; E2 no lo veía.

Modelo, vela k (decisión en k − 1, posición durante la vela k):
1. 30 balas: al empezar una campaña, escala Kb = w_b · Ep[k−1] / Es_b[k−1]; P&L y nocional = Kb × los de la corrida sola.
2. Presupuesto = Ep[k−1] − nocional real de balas. Por prioridad: los lotes fijos abiertos consumen su nocional actual;
   lo nuevo (lotes que abren y estrategias reescalables) recibe g = min(1, disponible / pedido).
   Lote nuevo: K = w · g · Ep[k−1] / Es[k−1] sobre los dólares de la corrida sola, fijo hasta que cierra.
   Reescalable: aporta w · g · Ep[k−1] · r_sola[k].
3. Tope: si el nocional total supera 1,05 × presupuesto, se recorta desde la última prioridad (proporcional dentro de
   la estrategia; en los lotes fijos el recorte queda).
"""
import numpy as np
import pandas as pd

FIJAS = {"ab_cortos", "mom_alts", "sold_btc"}
TOPE = 1.0
HOLGURA = 1.05


class Estrategia:
    def __init__(self, nombre, serie, lotes, idx):
        self.nombre = nombre
        s = serie.reindex(idx)
        E = s.E.values / s.E.values[0]
        E0 = np.r_[1.0, E[:-1]]
        self.tipo = "balas" if nombre == "balas5" else ("fijo" if nombre in FIJAS else "reesc")
        T = len(idx)
        n = s[[c for c in s.columns if c.startswith("n_")]].sum(axis=1).values / s.E.values[0]
        if self.tipo == "reesc":
            self.r = E / E0 - 1
            self.rp = np.minimum(s.E_peor.values / s.E.values[0] / E0 - 1, self.r)
            self.x = n / E0                       # exposición sola durante la vela (pedido con peso 1)
        elif self.tipo == "balas":
            self.dP = np.diff(np.r_[1.0, E])                         # dólares normalizados por vela
            self.dPp = s.E_peor.values / s.E.values[0] - E0
            self.noc = n
            activo = n > 0
            self.inicio = activo & ~np.r_[False, activo[:-1]]        # primera vela de cada campaña
            self.Es0 = E0
        else:
            # lotes: k de apertura, dólares por vela (normalizados por el capital inicial de la corrida sola)
            k_idx = {t: i for i, t in enumerate(idx)}
            lv = lotes[lotes.t.isin(k_idx)].copy()
            lv["k"] = lv.t.map(k_idx)
            c0 = s.E.values[0]
            self.lotes = []
            for lid, g in lv.sort_values("k").groupby("lote", sort=False):
                k = g.k.values; cum = g.cum.values / c0; cp = g.cum_peor.values / c0; noc = g.noc.values / c0
                d = np.diff(np.r_[0.0, cum]); dp = cp - np.r_[0.0, cum[:-1]]
                self.lotes.append(dict(k0=int(k[0]), k=k, d=d, dp=dp, noc=noc, Es0=E0[k[0]]))
            self.abre = {}                       # k -> lotes que abren en k
            for i, L in enumerate(self.lotes):
                self.abre.setdefault(L["k0"], []).append(i)
            # vela -> (lote, posición dentro del lote)
            self.activos = [[] for _ in range(T)]
            for i, L in enumerate(self.lotes):
                for j, kk in enumerate(L["k"]):
                    self.activos[kk].append((i, j))


class Cartera:
    def __init__(self, series, lotes, nombres):
        """series/lotes: dict estrategia -> DataFrame de la corrida sola (lotes sólo para las fijas). nombres en orden de
        prioridad (el de la búsqueda); balas5 se procesa primero como en el motor."""
        idx = None
        for k in nombres:
            idx = series[k].index if idx is None else idx.intersection(series[k].index)
        self.idx = idx
        self.nombres = list(nombres)
        self.est = [Estrategia(k, series[k], lotes.get(k), idx) for k in nombres]
        d = idx.floor("D").values
        self.inicios = np.r_[0, np.nonzero(d[1:] != d[:-1])[0] + 1]

    def simular(self, W):
        """W: (n, K) pesos en el orden de `nombres`. Devuelve ret, retp: (n, T) retornos por vela de la cartera."""
        W = np.atleast_2d(np.asarray(W, float))
        n, T = W.shape[0], len(self.idx)
        Ep = np.ones(n)
        ret = np.zeros((n, T)); retp = np.zeros((n, T))
        K = {}                                    # (estrategia, lote) -> escala (n,)
        Kb = np.zeros(n)
        bal = [(j, e) for j, e in enumerate(self.est) if e.tipo == "balas"]
        otros = [(j, e) for j, e in enumerate(self.est) if e.tipo != "balas"]
        for k in range(T):
            pnl = np.zeros(n); pnlp = np.zeros(n)
            reserva = np.zeros(n)
            for j, e in bal:
                if e.inicio[k]:
                    Kb = W[:, j] * Ep / e.Es0[k]
                reserva = Kb * e.noc[k - 1] if k > 0 else np.zeros(n)
                pnl += Kb * e.dP[k]; pnlp += Kb * e.dPp[k]
            restante = TOPE * Ep - reserva
            usado = []                            # (j, e, nocional (n,), [lotes nuevos/reesc]) para el tope
            g_re = {}
            for j, e in otros:
                w = W[:, j]
                if e.tipo == "reesc":
                    fijo = np.zeros(n); flex = w * e.x[k] * Ep
                    nuevos = []
                else:
                    fijo = np.zeros(n)
                    for (i, p) in e.activos[k]:
                        if p > 0:                 # abierto antes: su nocional actual (al cierre de la vela anterior)
                            fijo += K[(j, i)] * e.lotes[i]["noc"][p - 1]
                    nuevos = e.abre.get(k, [])
                    flex = np.zeros(n)
                    for i in nuevos:
                        L = e.lotes[i]
                        flex += w * Ep / L["Es0"] * L["noc"][0]
                disp = np.maximum(restante - fijo, 0.0)
                g = np.where(flex > 1e-15, np.minimum(1.0, disp / np.where(flex > 1e-15, flex, 1.0)), 1.0)
                g = np.where(w > 0, g, 0.0)
                for i in nuevos:
                    L = e.lotes[i]
                    K[(j, i)] = w * g * Ep / L["Es0"]
                if e.tipo == "reesc":
                    g_re[j] = g
                restante = restante - fijo - g * flex
                usado.append((j, e, fijo + g * flex))
            # tope: recorta desde la última prioridad
            total = sum(u[2] for u in usado)
            exceso = total - HOLGURA * (TOPE * Ep - reserva)
            if np.any(exceso > 1e-12):
                exceso = np.maximum(exceso, 0.0)
                for j, e, noc in reversed(usado):
                    corte = np.minimum(exceso, noc)
                    f = np.where(noc > 1e-15, 1 - corte / np.where(noc > 1e-15, noc, 1.0), 1.0)
                    if e.tipo == "reesc":
                        g_re[j] = g_re[j] * f
                    else:
                        for (i, p) in e.activos[k]:
                            K[(j, i)] = K[(j, i)] * f
                    exceso = exceso - corte
                    if not np.any(exceso > 1e-12):
                        break
            # P&L de la vela
            for j, e in otros:
                if e.tipo == "reesc":
                    pnl += W[:, j] * g_re[j] * Ep * e.r[k]; pnlp += W[:, j] * g_re[j] * Ep * e.rp[k]
                else:
                    for (i, p) in e.activos[k]:
                        L = e.lotes[i]; Ki = K[(j, i)]
                        pnl += Ki * L["d"][p]; pnlp += Ki * L["dp"][p]
                        if p == len(L["k"]) - 1:
                            del K[(j, i)]
            ret[:, k] = pnl / Ep; retp[:, k] = np.minimum(pnlp / Ep, ret[:, k])
            Ep = Ep + pnl
        return ret, retp


def cargar(rutas_series, rutas_lotes, nombres):
    S = {k: pd.read_pickle(rutas_series[k]) for k in nombres}
    L = {k: pd.read_pickle(rutas_lotes[k]) for k in nombres if k in FIJAS}
    return Cartera(S, L, nombres)
