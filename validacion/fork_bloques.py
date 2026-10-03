"""Rama fork-bloques: cartera sin capital compartido, salvo el par cortos Aberration + momentum alts, que comparten
peso y el filtro de tendencia (ROC de BTC a 90 días: > 0 sólo largos, < 0 sólo cortos) y no pueden estar en sentidos
opuestos. Cada bloque pide peso × patrimonio total, sin cascada (tope de seguridad 3×, el apalancamiento del exchange).

SÓLO IS (2020-01-01 → 2023-12-31): el cargador no entrega nada posterior. Mismo método que el protocolo §5 (E1, E10):
búsqueda con la cartera por lotes, p95 de la caída en 2000 remuestreos ≤ nivel, verificación con el motor completo.

    python -m validacion.fork_bloques solos   --datos … --top50 … --resumen … --universo … --xbt …
    python -m validacion.fork_bloques elegir
    python -m validacion.fork_bloques verificar --datos … --top50 … --resumen … --universo … --xbt … [--niveles 20,30]
"""
import argparse
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from . import pesos as P
from .cartera_lotes import cargar
from .correr_is import funding_xbt
from .datos import CORTE_IS, Paquete
from .motor_bt import TOPE_BLOQUES, Corrida, metricas
from .verificar_motor import p95_motor

RES = Path(__file__).resolve().parent / "resultados" / "fork_bloques"
BLOQUES = {"par_alts": {"ab_cortos": 1.0, "mom_alts": 1.0}, "balas5": {"balas5": 1.0}, "rsi2_btc": {"rsi2_btc": 1.0},
           "wr2": {"wr2": 1.0}, "sold_btc": {"sold_btc": 1.0}, "rsi2_eth": {"rsi2_eth": 1.0}}
NOMBRES = list(BLOQUES)
NIVELES = [0.10, 0.20, 0.25, 0.30]


def a_pesos(w):
    """Pesos de bloque → pesos del motor (los dos miembros del par llevan el peso del par)."""
    d = dict(zip(NOMBRES, map(float, w)))
    out = {k: d[k] for k in NOMBRES if k != "par_alts"}
    out["ab_cortos"] = out["mom_alts"] = d["par_alts"]
    return out


def _solo(args):
    k, a = args
    p = Paquete(a.datos)
    fx = funding_xbt(a.xbt, p, CORTE_IS) if k == "balas5" else None
    r = Corrida(p, a.top50, a.resumen, a.universo, BLOQUES[k], modo="IS", corrida="A", funding_xbt=fx, log_cada=0,
                registro=True, variante="bloques").correr()
    r.serie.to_pickle(RES / f"is_A_{k}.pkl")
    r.lotes_vela.to_pickle(RES / f"is_A_{k}_lotes.pkl")
    m = metricas(r.serie)
    ops = r.operaciones()
    m.update(lotes=len(ops), por_estrategia={e: int(n) for e, n in ops.groupby("estrategia").size().items()} if len(ops) else {})
    if k == "par_alts" and len(ops):        # el par nunca debe tener cortos y largos abiertos a la vez
        L = ops.assign(fin=ops.cerrado_ts.fillna(ops.abierto_ts.max() + 1))
        cortos, largos = L[L.lado < 0], L[L.lado > 0]
        m["solapes_cortos_largos"] = int(sum(((largos.abierto_ts < c.fin) & (largos.fin > c.abierto_ts)).sum()
                                             for c in cortos.itertuples()))
    return k, m


def solos(a):
    RES.mkdir(parents=True, exist_ok=True)
    out = {}
    with ProcessPoolExecutor(a.procesos) as ex:
        hechos = json.loads((RES / "solos_is.json").read_text()) if (RES / "solos_is.json").exists() else {}
        faltan = [k for k in NOMBRES if not (RES / f"is_A_{k}.pkl").exists() or not (RES / f"is_A_{k}_lotes.pkl").exists()]
        out.update({k: v for k, v in hechos.items() if k not in faltan})
        for k, m in ex.map(_solo, [(k, a) for k in faltan]):
            out[k] = m
            print(f"{k:9s} tasa {m['cagr']:7.1%}  caída pesimista {m['dd_pesimista']:6.1%}  estricta {m['dd_estricta']:6.1%}"
                  f"  Sharpe {m['sharpe']:.2f}  lotes {m['lotes']}" + (f"  solapes {m.get('solapes_cortos_largos')}" if k == "par_alts" else ""),
                  flush=True)
            (RES / "solos_is.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=float))


def _cart():
    return cargar({k: RES / f"is_A_{k}.pkl" for k in NOMBRES}, {k: RES / f"is_A_{k}_lotes.pkl" for k in NOMBRES},
                  NOMBRES, cascada=False, tope=TOPE_BLOQUES)


def _buscar(L):
    w, c, p = P.buscar_lotes(_cart(), L, log=lambda m: print(f"[{L:.0%}]{m}", flush=True))
    return L, w, c, p


def elegir(a):
    out = dict(rama="fork-bloques", tramo="IS 2020-01-01 → 2023-12-31", corrida="A", niveles={})
    with ProcessPoolExecutor(a.procesos) as ex:
        res = {L: (w, c, p) for L, w, c, p in ex.map(_buscar, NIVELES)}
    cart = _cart()
    for L in NIVELES:
        w2, c2, p2, f = P.verificar_lotes(res[L][0], cart, L)
        ret, retp = cart.simular(w2[None, :]); ret, retp = ret[0], retp[0]
        eq = np.cumprod(1 + ret); eqp = np.r_[1.0, eq[:-1]] * (1 + retp)
        out["niveles"][f"{int(L * 100)}"] = dict(pesos_bloque=dict(zip(NOMBRES, map(float, w2))), pesos=a_pesos(w2),
                                                 tasa_is=float(c2), p95_is=float(p2), escala=float(f),
                                                 caida_historica_pesimista_is=float(np.min(eqp / np.maximum.accumulate(eq) - 1)))
        print(f"nivel {L:.0%}: tasa IS {c2:.1%}  p95 {p2:.1%}  {dict(zip(NOMBRES, np.round(w2, 3)))}", flush=True)
    (RES / "seleccion_is.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))


def _corrida(args):
    w, corrida, a = args
    p = Paquete(a.datos)
    r = Corrida(p, a.top50, a.resumen, a.universo, w, modo="IS", corrida=corrida, funding_xbt=funding_xbt(a.xbt, p, CORTE_IS),
                log_cada=0, variante="bloques").correr()
    m = metricas(r.serie); m["p95"] = p95_motor(r.serie)
    return r.serie, m


def verificar(a):
    """Motor completo (A y B). Si el p95 de A pasa el nivel, busca el mayor factor común que cumple (secante log-log)."""
    sel = json.loads((RES / "seleccion_is.json").read_text())
    for nivel in a.niveles.split(","):
        L = int(nivel) / 100
        w0 = sel["niveles"][nivel]["pesos"]
        pts = {}
        f = 1.0
        for _ in range(6):
            serie, m = _corrida(({k: v * f for k, v in w0.items()}, "A", a))
            pts[f] = (m, serie)
            print(f"nivel {nivel} f={f:.4f}: A tasa {m['cagr']:.1%} caída {m['dd_pesimista']:.1%} p95 {m['p95']:.1%}", flush=True)
            ok = [g for g in pts if pts[g][0]["p95"] <= L]; no = [g for g in pts if pts[g][0]["p95"] > L]
            if not no or (ok and pts[max(ok)][0]["p95"] >= L - 0.01):
                break
            hi = min(no); lo = max(ok) if ok else None
            if lo is None:
                f = round(hi * (L * 0.995 / pts[hi][0]["p95"]), 4)
            else:
                b = np.log(pts[hi][0]["p95"] / pts[lo][0]["p95"]) / np.log(hi / lo)
                f = round(min(max(lo * (L * 0.995 / pts[lo][0]["p95"]) ** (1 / max(b, 0.2)), lo + 0.25 * (hi - lo)),
                              hi - 0.1 * (hi - lo)), 4)
        ok = [g for g in pts if pts[g][0]["p95"] <= L]
        f = max(ok) if ok else min(pts)
        w = {k: v * f for k, v in w0.items()}
        mA, sA = pts[f]
        sB, mB = _corrida((w, "B", a))
        sA.to_pickle(RES / f"is_motor_A_nivel{nivel}.pkl"); sB.to_pickle(RES / f"is_motor_B_nivel{nivel}.pkl")
        print(f"nivel {nivel} FINAL f={f:.4f}: A tasa {mA['cagr']:.1%} caída {mA['dd_pesimista']:.1%} p95 {mA['p95']:.1%} | "
              f"B tasa {mB['cagr']:.1%} caída {mB['dd_pesimista']:.1%} estricta {mB['dd_estricta']:.1%} p95 {mB['p95']:.1%}", flush=True)
        (RES / f"verificacion_{nivel}.json").write_text(json.dumps(dict(nivel=nivel, factor=f, pesos=w, A=mA, B=mB,
                                                                         busqueda={str(g): v[0]["p95"] for g, v in pts.items()}),
                                                                    indent=1, ensure_ascii=False, default=float))


def _prueba(args):
    nivel, w, a = args
    from .datos import FIN_OOS
    p = Paquete(a.datos)
    r = Corrida(p, a.top50, a.resumen, a.universo, w, modo="OOS", corrida="B", desde=CORTE_IS, hasta=FIN_OOS,
                funding_xbt=funding_xbt(a.xbt, p, FIN_OOS), log_cada=0, variante="bloques").correr()
    r.serie.to_pickle(RES / f"prueba_B_nivel{nivel}.pkl")
    m = metricas(r.serie)
    e = r.serie.E.resample("YE").last()
    e0 = np.r_[r.serie.E.iat[0], e.values[:-1]]
    m["por_año"] = {str(i.year): float(v / v0 - 1) for i, v, v0 in zip(e.index, e.values, e0)}
    ops = r.operaciones()
    m.update(lotes=len(ops), funding=float(r.serie.funding.iat[-1]),
             comisiones=float(r.db.filas("SELECT COALESCE(SUM(comision),0) s FROM operaciones")[0]["s"]),
             por_estrategia={k: float(v) for k, v in ops.groupby("estrategia").pnl.sum().items()} if len(ops) else {})
    par = ops[ops.estrategia.isin(["ab_cortos", "mom_alts"])] if len(ops) else ops
    if len(par):
        L = par.assign(fin=par.cerrado_ts.fillna(par.abierto_ts.max() + 1))
        cortos, largos = L[L.lado < 0], L[L.lado > 0]
        m["solapes_cortos_largos"] = int(sum(((largos.abierto_ts < c.fin) & (largos.fin > c.abierto_ts)).sum()
                                             for c in cortos.itertuples()))
    return nivel, m


def prueba(a):
    """Informativo (pedido del usuario, 2026-10-03): los pesos del fork, elegidos sólo con 2020–2023, en 2024 → sep 2026.
    El período ya se había visto para la cascada validada: no es una prueba limpia del fork y no sirve para elegirlo."""
    from .datos import limite
    limite("OOS")
    oos = json.loads((RES.parent / "resultado_oos.json").read_text())
    calmar_btc = oos["casos"]["btc_tendencia_B"]["calmar"]
    trabajos = [(n, json.loads((RES / f"verificacion_{n}.json").read_text())["pesos"], a) for n in a.niveles.split(",")]
    out = {}
    with ProcessPoolExecutor(a.procesos) as ex:
        for n, m in ex.map(_prueba, trabajos):
            p95 = json.loads((RES / f"verificacion_{n}.json").read_text())["B"]["p95"]
            m["criterios"] = dict(C1=m["cagr"] > 0, C2=-m["dd_pesimista"] <= p95, C3=(m["calmar"] or -9) >= calmar_btc,
                                  p95_is=p95, calmar_btc_tendencia=calmar_btc)
            out[n] = m
            print(f"nivel {n}: tasa {m['cagr']:.1%}  caída pesimista {m['dd_pesimista']:.1%}  estricta {m['dd_estricta']:.1%}  "
                  f"Calmar {m['calmar'] or 0:.2f}  años {[f'{v:.0%}' for v in m['por_año'].values()]}  criterios {m['criterios']}",
                  flush=True)
    (RES / "prueba_2024_2026.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=float))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("paso", choices=["solos", "elegir", "verificar", "prueba"])
    for k in ("datos", "top50", "resumen", "universo", "xbt"):
        ap.add_argument("--" + k)
    ap.add_argument("--procesos", type=int, default=2); ap.add_argument("--niveles", default="10,20,25,30")
    a = ap.parse_args()
    {"solos": solos, "elegir": elegir, "verificar": verificar, "prueba": prueba}[a.paso](a)
