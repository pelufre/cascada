"""Resumen de la revalidación (E14): criterios C1–C3 de cada nivel con el motor corregido, comparación con lo medido
antes de las correcciones, retornos por año, atribución, exposición, intervalo de la ventaja y referencias."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import pesos as P

RAIZ = Path(__file__).resolve().parent
RES = RAIZ / "resultados"
REV = RES / "revalidacion"
NIVELES = ["10", "20", "25", "30"]


def _j(nombre):
    p = REV / f"{nombre}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _diarios(nombre):
    s = pd.read_pickle(REV / f"{nombre}.pkl").E
    return np.log(s.resample("D").last().dropna()).diff().dropna()


def intervalo_ventaja(cartera, referencia, n=2000):
    """Remuestreo por bloques estacionarios (bloque medio 20 días, semilla 12345) de los retornos diarios de la prueba,
    pareados: tasa anual de la cartera, de la referencia y su diferencia. Percentiles 5/50/95 y P(diferencia > 0)."""
    a = _diarios(cartera); b = _diarios(referencia)
    comunes = a.index.intersection(b.index)
    a, b = a.loc[comunes].values, b.loc[comunes].values
    R = P.remuestreos(len(a), n)
    ta = np.exp(a[R].sum(axis=1) * 365 / len(a)) - 1
    tb = np.exp(b[R].sum(axis=1) * 365 / len(a)) - 1
    d = ta - tb
    q = lambda x: [float(np.percentile(x, k)) for k in (5, 50, 95)]
    return dict(cartera=q(ta), referencia=q(tb), diferencia=q(d), prob_diferencia_positiva=float((d > 0).mean()),
                dias=len(a), nota="remuestreo de la prueba: mide la incertidumbre muestral, no corrige la investigación previa")


def spot_mantener(paquete_dir, base, desde, hasta, comision=0.001):
    """Comprar `base` al contado al empezar y mantenerla (sin funding ni rebalanceo): cierres del perpetuo como precio."""
    c = pd.read_csv(Path(paquete_dir) / "_cierres_4h_perp.csv", index_col=0)
    c.index = pd.to_datetime(c.index, utc=True).tz_localize(None)
    s = c[base].dropna()
    s = s[(s.index >= pd.Timestamp(desde) - pd.Timedelta(hours=4)) & (s.index < pd.Timestamp(hasta))]
    s.index = s.index + pd.Timedelta(hours=4)               # fila = cierre de la vela
    E = (s / s.iat[0]) * (1 - comision)
    años = (E.index[-1] - E.index[0]).total_seconds() / (365.25 * 86400)
    return dict(cagr=float(E.iat[-1] ** (1 / años) - 1), dd_optimista=float((E / E.cummax() - 1).min()))


def resumir(cong, a=None):
    viejo_oos = json.loads((RES / "resultado_oos.json").read_text())
    out = dict(pesos_commit="54ddf76", motor="1.3.1 (correcciones de la segunda auditoría, E14, y entrada salteada de momentum alts, E15)", niveles={})
    bt = _j("btc_tendencia_OOS_B")
    for n in NIVELES:
        isB, ooB = _j(f"n{n}_IS_B"), _j(f"n{n}_OOS_B")
        if not (isB and ooB):
            continue
        p95_cong = cong["verificacion_motor"][n]["resultados"]["B"]["p95"]
        viejo = viejo_oos["casos"]["cartera_B"] if n == cong["nivel_elegido"] else \
            json.loads((RES / f"resultado_oos_nivel{n}.json").read_text())["casos"][f"cartera_nivel{n}_B"]
        crit = dict(
            C1=dict(ok=ooB["cagr"] > 0, valor=ooB["cagr"]),
            C2=dict(ok=-ooB["dd_pesimista"] <= isB["p95"], valor=ooB["dd_pesimista"], limite=-isB["p95"],
                    estricta=ooB["dd_estricta"], estricta_ok=-ooB["dd_estricta"] <= isB["p95"],
                    limite_congelado=-p95_cong, ok_con_limite_congelado=-ooB["dd_pesimista"] <= p95_cong),
            C3=dict(ok=bt is not None and (ooB["calmar"] or -9) >= (bt["calmar"] or -9), cartera=ooB["calmar"],
                    btc_tendencia=bt and bt["calmar"]))
        out["niveles"][n] = dict(
            IS_B={k: isB[k] for k in ("cagr", "dd_optimista", "dd_pesimista", "dd_estricta", "sharpe", "calmar", "p95",
                                      "nocional_max", "exposicion_max")},
            OOS_B={k: ooB[k] for k in ("cagr", "dd_optimista", "dd_pesimista", "dd_estricta", "sharpe", "calmar",
                                       "nocional_max", "exposicion_max", "funding", "comisiones")},
            antes=dict(IS_B=dict(cagr=cong["verificacion_motor"][n]["resultados"]["B"]["cagr"], p95=p95_cong),
                       OOS_B=dict(cagr=viejo["cagr"], dd_pesimista=viejo["dd_pesimista"], calmar=viejo["calmar"],
                                  sharpe=viejo["sharpe"])),
            por_año_IS=isB["por_año"], por_año_OOS=ooB["por_año"], atribucion_OOS=ooB["atribucion"],
            lotes_OOS={k: ooB.get(k) for k in ("lotes", "lotes_cerrados", "lotes_abiertos", "ganadoras", "factor_beneficio",
                                               "peor_lote", "campañas_balas")},
            criterios=crit, aprobada=all(v["ok"] for v in crit.values()))
        for c in ("A",):
            x, y = _j(f"n{n}_IS_{c}"), _j(f"n{n}_OOS_{c}")
            if x and y:
                out["niveles"][n][f"IS_{c}"] = {k: x[k] for k in ("cagr", "dd_pesimista", "dd_estricta", "p95")}
                out["niveles"][n][f"OOS_{c}"] = {k: y[k] for k in ("cagr", "dd_pesimista", "dd_estricta", "calmar")}
        for tr in ("IS", "OOS"):
            x = _j(f"n{n}_{tr}_B_operable")
            if x:
                out["niveles"][n][f"{tr}_B_operable_kucoin"] = {k: x[k] for k in ("cagr", "dd_pesimista", "dd_estricta", "calmar", "p95")}
        if bt:
            try:
                out["niveles"][n]["intervalo_ventaja_vs_btc_tendencia"] = intervalo_ventaja(f"n{n}_OOS_B", "btc_tendencia_OOS_B")
            except Exception as e:
                out["niveles"][n]["intervalo_ventaja_vs_btc_tendencia"] = str(e)
    comp = {}
    for k in ("btc_tendencia", "btc_mantener", "eth_mantener", "igual_riesgo", "sin_balas", "sin_alts"):
        x = _j(f"{k}_OOS_B")
        if x:
            comp[k] = {c: x[c] for c in ("cagr", "dd_pesimista", "dd_estricta", "calmar", "sharpe")}
    if a is not None:
        for b in ("BTC", "ETH"):
            try:
                comp[f"{b.lower()}_spot_mantener"] = spot_mantener(a.datos, b, "2024-01-01", "2026-10-01")
            except Exception as e:
                comp[f"{b.lower()}_spot_mantener"] = str(e)
    out["comparaciones_OOS_B"] = comp
    (REV / "revalidacion.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=float))
    print(json.dumps({n: dict(IS=v["IS_B"]["cagr"], p95=v["IS_B"]["p95"], OOS=v["OOS_B"]["cagr"], dd=v["OOS_B"]["dd_pesimista"],
                              aprobada=v["aprobada"]) for n, v in out["niveles"].items()}, indent=1, default=float))
    return out
