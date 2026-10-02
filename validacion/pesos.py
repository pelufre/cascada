"""Elección de pesos con IS (protocolo §5 y enmienda E1).

A partir de las corridas de cada estrategia sola (peso 1) se reconstruye la cartera cada 4 h con su exposición real
(nocional / patrimonio) y el reparto en cascada con tope total de 1×; así la búsqueda es rápida. Los pesos elegidos se
verifican después con el motor completo (§5.4).

Restricción: percentil 95 de la caída máxima en remuestreos por bloques estacionarios (bloque medio 20 días) de los
retornos diarios ≤ L. Objetivo: máxima tasa anual compuesta.
"""
import numpy as np
import pandas as pd

PASO = 0.025
SEMILLA = 12345


def series(rutas):
    """rutas: estrategia -> parquet de su corrida sola. Devuelve índice común y matrices r, r_peor, x (tiempo × estrategia)."""
    S = {k: pd.read_pickle(p) for k, p in rutas.items()}
    idx = None
    for s in S.values():
        idx = s.index if idx is None else idx.intersection(s.index)
    nombres = list(S)
    r = np.zeros((len(idx), len(nombres))); rp = np.zeros_like(r); x = np.zeros_like(r)
    for j, k in enumerate(nombres):
        s = S[k].loc[idx]
        E = s.E.values; E0 = np.r_[E[0], E[:-1]]
        r[:, j] = E / E0 - 1
        rp[:, j] = np.minimum(s.E_peor.values / E0 - 1, r[:, j])
        n = s[[c for c in s.columns if c.startswith("n_")]].sum(axis=1).values
        x[:, j] = n / E0          # nocional durante la vela (posiciones registradas antes del ciclo) sobre el patrimonio previo
    return idx, nombres, r, rp, x


def cartera(w, r, rp, x, tope=1.0):
    """Retorno cada 4 h de la cartera con el reparto en cascada (orden de columnas = prioridad)."""
    resto = np.full(r.shape[0], tope)
    ret = np.zeros(r.shape[0]); retp = np.zeros(r.shape[0])
    for j, wj in enumerate(w):
        if wj <= 0:
            continue
        nec = wj * x[:, j]
        asig = np.minimum(nec, resto)
        g = np.where(nec > 1e-12, asig / np.where(nec > 1e-12, nec, 1), 1.0)
        resto = resto - asig
        ret += wj * g * r[:, j]; retp += wj * g * rp[:, j]
    return ret, retp


def inicios_dia(idx):
    d = idx.floor("D").values
    return np.r_[0, np.nonzero(d[1:] != d[:-1])[0] + 1]


def diarios(ret, idx, inicios=None):
    lr = np.log1p(np.maximum(ret, -0.999999))
    return np.add.reduceat(lr, inicios if inicios is not None else inicios_dia(idx))      # log-retorno diario


def remuestreos(n_dias, n, bloque=20, semilla=SEMILLA):
    """Índices de remuestreo por bloques estacionarios (Politis-Romano): matriz n × n_dias."""
    rng = np.random.default_rng(semilla)
    idx = np.empty((n, n_dias), dtype=np.int64)
    p = 1.0 / bloque
    idx[:, 0] = rng.integers(0, n_dias, n)
    nuevo = rng.random((n, n_dias)) < p
    saltos = rng.integers(0, n_dias, (n, n_dias))
    for t in range(1, n_dias):
        idx[:, t] = np.where(nuevo[:, t], saltos[:, t], (idx[:, t - 1] + 1) % n_dias)
    return idx


def caida_p95(lr_dia, idx):
    caminos = np.cumsum(lr_dia[idx], axis=1)
    pico = np.maximum.accumulate(np.maximum(caminos, 0), axis=1)
    dd = 1 - np.exp(np.min(caminos - pico, axis=1))
    return float(np.percentile(dd, 95))


def tasa(ret, idx):
    años = (idx[-1] - idx[0]).total_seconds() / (365.25 * 86400)
    eq = np.exp(np.sum(np.log1p(np.maximum(ret, -0.999999))))
    return eq ** (1 / años) - 1


def preparar(idx, r, rp, x):
    return (idx, r, rp, x, inicios_dia(idx))


def evaluar(w, datos, R):
    idx, r, rp, x, ini = datos
    ret, _ = cartera(w, r, rp, x)
    return tasa(ret, idx), caida_p95(diarios(ret, idx, ini), R)


def _escala_max(d, L, datos, R):
    """Mayor escala s (con s·d ≤ 1) que cumple la restricción, por bisección."""
    hi = 1.0 / max(d.max(), 1e-9)
    if evaluar(d * hi, datos, R)[1] <= L:
        return hi
    lo = 0.0
    for _ in range(18):
        m = (lo + hi) / 2
        if evaluar(d * m, datos, R)[1] <= L:
            lo = m
        else:
            hi = m
    return lo


def a_grilla(w):
    return np.floor(np.asarray(w) / PASO + 1e-9) * PASO


def buscar(datos, L, n_dir=1500, n_rem=200, top=15, semilla=SEMILLA, fijos_cero=()):
    """Búsqueda de pesos (enmienda E1): direcciones al azar en el símplex, escala máxima que cumple L, redondeo a la grilla
    de 0,025 y descenso por coordenadas de ±0,025. Devuelve (pesos, tasa, p95) del mejor."""
    idx = datos[0]
    R = remuestreos(len(datos[4]), n_rem, semilla=semilla)
    K = datos[1].shape[1]
    rng = np.random.default_rng(semilla)
    cand = []
    for i in range(n_dir):
        activo = rng.random(K) < 0.7
        activo[list(fijos_cero)] = False
        if not activo.any():
            continue
        d = np.where(activo, rng.dirichlet(np.ones(K)), 0.0)
        s = _escala_max(d, L, datos, R)
        w = a_grilla(d * s)
        if w.sum() <= 0:
            continue
        c, p = evaluar(w, datos, R)
        if p <= L:
            cand.append((c, p, w))
    cand.sort(key=lambda z: -z[0])
    mejor = cand[0] if cand else (0.0, 0.0, np.zeros(K))
    for c0, p0, w0 in cand[:top]:
        w = w0.copy(); c = c0; p = p0
        mejora = True
        while mejora:
            mejora = False
            for j in range(K):
                if j in fijos_cero:
                    continue
                for paso in (PASO, -PASO):
                    w2 = w.copy(); w2[j] = min(max(w2[j] + paso, 0.0), 1.0)
                    if np.allclose(w2, w):
                        continue
                    c2, p2 = evaluar(w2, datos, R)
                    if p2 <= L and c2 > c + 1e-9:
                        w, c, p, mejora = w2, c2, p2, True
        if c > mejor[0]:
            mejor = (c, p, w)
    return mejor[2], mejor[0], mejor[1]


def verificar(w, datos, L, n_rem=2000, semilla=SEMILLA):
    """Chequeo final con 2000 remuestreos; si no cumple, escala todos los pesos por igual hasta cumplir (§5.4)."""
    idx = datos[0]
    R = remuestreos(len(datos[4]), n_rem, semilla=semilla)
    w = np.asarray(w, float)
    c, p = evaluar(w, datos, R)
    f = 1.0
    while p > L and f > 0.05:          # escala proporcional exacta (§5.4); redondear a la grilla aquí bajaría cada peso un paso
        f = round(f - 0.01, 2)
        c, p = evaluar(w * f, datos, R)
    return w * f, c, p, f


# ---- Enmienda E10: misma búsqueda (E1) evaluada con la cartera por lotes (validacion/cartera_lotes.py), en lote ----

def evaluar_lotes(W, cart, R):
    """W: (n, K). Devuelve tasas (n,) y p95 (n,) con los remuestreos R."""
    W = np.atleast_2d(W)
    ret, _ = cart.simular(W)
    lr = np.add.reduceat(np.log1p(np.maximum(ret, -0.999999)), cart.inicios, axis=1)
    años = (cart.idx[-1] - cart.idx[0]).total_seconds() / (365.25 * 86400)
    tasas = np.exp(lr.sum(axis=1)) ** (1 / años) - 1
    p95 = np.array([caida_p95(l, R) for l in lr])
    return tasas, p95


def buscar_lotes(cart, L, n_dir=1500, n_rem=200, top=15, semilla=SEMILLA, log=print):
    K = len(cart.nombres)
    R = remuestreos(len(cart.inicios), n_rem, semilla=semilla)
    rng = np.random.default_rng(semilla)
    D = []
    for _ in range(n_dir):                       # mismo orden de sorteo que buscar()
        activo = rng.random(K) < 0.7
        if not activo.any():
            continue
        D.append(np.where(activo, rng.dirichlet(np.ones(K)), 0.0))
    D = np.array(D)
    hi = 1.0 / np.maximum(D.max(axis=1), 1e-9)
    _, p = evaluar_lotes(D * hi[:, None], cart, R)
    escala = np.where(p <= L, hi, 0.0)
    pend = p > L
    lo = np.zeros(len(D)); h = hi.copy()
    for it in range(18):                         # bisección de todas las direcciones a la vez
        m = (lo + h) / 2
        _, p = evaluar_lotes(D[pend] * m[pend, None], cart, R)
        ok = np.zeros(len(D), bool); ok[np.nonzero(pend)[0]] = p <= L
        lo = np.where(pend & ok, m, lo); h = np.where(pend & ~ok, m, h)
    escala = np.where(pend, lo, escala)
    Wg = a_grilla(D * escala[:, None])
    Wg = Wg[Wg.sum(axis=1) > 0]
    c, p = evaluar_lotes(Wg, cart, R)
    f = p <= L
    cand = sorted(zip(c[f], p[f], Wg[f]), key=lambda z: -z[0])
    log(f"  nivel {L:.0%}: {len(cand)} candidatos factibles; mejor inicial {cand[0][0]:.1%}" if cand else "  sin candidatos")
    mejor = cand[0] if cand else (0.0, 0.0, np.zeros(K))
    actuales = [list(z) for z in cand[:top]]
    while actuales:                              # descenso por coordenadas ±0,025, todos los puntos a la vez
        vec = []
        for a_i, (c0, p0, w0) in enumerate(actuales):
            for j in range(K):
                for paso in (PASO, -PASO):
                    w2 = w0.copy(); w2[j] = min(max(w2[j] + paso, 0.0), 1.0)
                    if not np.allclose(w2, w0):
                        vec.append((a_i, w2))
        c2, p2 = evaluar_lotes(np.array([v[1] for v in vec]), cart, R)
        nuevos = []
        for a_i, (c0, p0, w0) in enumerate(actuales):
            mejores = [(c2[i], p2[i], vec[i][1]) for i in range(len(vec)) if vec[i][0] == a_i and p2[i] <= L and c2[i] > c0 + 1e-9]
            if mejores:
                nuevos.append(list(max(mejores, key=lambda z: z[0])))
            elif c0 > mejor[0]:
                mejor = (c0, p0, w0)
        actuales = nuevos
    return mejor[2], mejor[0], mejor[1]


def verificar_lotes(w, cart, L, n_rem=2000, semilla=SEMILLA):
    """Chequeo con 2000 remuestreos; si no cumple, escala común exacta (§5.4)."""
    R = remuestreos(len(cart.inicios), n_rem, semilla=semilla)
    w = np.asarray(w, float)
    fs = np.round(np.arange(1.0, 0.04, -0.01), 2)
    c, p = evaluar_lotes(w[None, :] * fs[:, None], cart, R)
    i = int(np.argmax(p <= L)) if np.any(p <= L) else len(fs) - 1
    return w * fs[i], float(c[i]), float(p[i]), float(fs[i])
