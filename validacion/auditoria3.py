"""Evidencia para la respuesta a la segunda auditoría (2026-10-03), con el motor 1.3 (E14). Diagnóstico: no cambia
pesos, niveles ni reglas (protocolo §8).

1. Descomposición 1.0 → 1.2/1.3 en el mismo período (2024-01-01 → 2026-09-30), corrida B, rehecha con el motor
   corregido y con «sin costos ni funding» de verdad (también en 30 balas, que antes conservaba sus costos y una tasa
   de funding por defecto):
     A  1.0 declarada (serie de investigación, sin cambios)
     B  motor 1.3, pesos 1.0, velas spot, sin funding, con costos
     C  motor 1.3, pesos 1.0, perpetuos + funding, con costos
     E  motor 1.3, pesos 1.0, perpetuos, sin costos NI funding en las dos cuentas
     D  motor 1.3, pesos congelados (= revalidación, resultados/revalidacion/n{20,30}_OOS_B.json)
   Las diferencias entre pasos no son aportes causales que se sumen: cambian trayectoria, capital, señales y tamaños.
2. Lotes del nivel 30 (IS y prueba, corrida B) para revisar símbolos sin contrato en KuCoin, exposición durante los
   huecos de datos de 2022 (E9) y durante los eventos de funding extremos.

    CASCADA_ABRIR_OOS=1 python -m validacion.auditoria3 --perp … --spot … --top50 … --resumen … --universo … --xbt …
"""
import argparse
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from .auditoria2 import PESOS_10, declarado_10
from .correr_is import funding_xbt
from .datos import CONGELADO, CORTE_IS, FIN_OOS, INICIO_IS, Paquete, limite
from .motor_bt import Corrida, metricas

RAIZ = Path(__file__).resolve().parent
RES = RAIZ / "resultados" / "auditoria3"


def _correr(args):
    clave, pesos, datos, desde, hasta, extra, a = args
    salida = RES / f"{clave}.json"
    if salida.exists():
        return clave, json.loads(salida.read_text())
    p = Paquete(a.perp if datos == "perp" else a.spot)
    fx = funding_xbt(a.xbt, p, hasta) if datos == "perp" else None
    modo = "IS" if hasta <= CORTE_IS else "OOS"
    r = Corrida(p, a.top50, a.resumen, a.universo, pesos, modo=modo, corrida="B", desde=desde, hasta=hasta,
                funding_xbt=fx, log_cada=0, **extra).correr()
    r.serie.to_pickle(RES / f"{clave}.pkl")
    r.operaciones().to_pickle(RES / f"{clave}_lotes.pkl")
    pd.DataFrame(r.db.filas("SELECT * FROM operaciones")).to_pickle(RES / f"{clave}_ops.pkl")
    m = metricas(r.serie)
    at = r.info["atribucion"]
    m["incidencias"] = {x["tipo"]: x["n"] for x in r.db.filas("SELECT tipo, COUNT(*) n FROM incidencias GROUP BY tipo")}
    m.update(atribucion=at, lotes=len(r.operaciones()),
             comisiones=at["comisiones_principal"] + (at.get("balas") or {}).get("comisiones", 0.0),
             funding=at["funding_principal"] + (at.get("balas") or {}).get("funding", 0.0))
    salida.write_text(json.dumps(m, indent=1, ensure_ascii=False, default=float))
    return clave, m


def main(a):
    limite("OOS")
    RES.mkdir(parents=True, exist_ok=True)
    cong = json.loads(CONGELADO.read_text())
    T = [("D_n30_is_lotes", cong["pesos_por_nivel"]["30"], "perp", INICIO_IS, CORTE_IS, {}, a),
         ("D_n30_oos_lotes", cong["pesos_por_nivel"]["30"], "perp", CORTE_IS, FIN_OOS, {}, a)]
    for n in ("30", "20"):
        T += [(f"E_n{n}_pesos10_perp_sin_costos_ni_funding", PESOS_10[n], "perp", CORTE_IS, FIN_OOS,
               dict(sin_costos=True, sin_funding=True), a),
              (f"C_n{n}_pesos10_perp", PESOS_10[n], "perp", CORTE_IS, FIN_OOS, {}, a),
              (f"B_n{n}_pesos10_spot", PESOS_10[n], "spot", CORTE_IS, FIN_OOS, {}, a)]
    out = {f"A_n{n}_declarado_1.0": declarado_10(n) for n in ("20", "30")}
    with ProcessPoolExecutor(a.procesos) as ex:
        for clave, m in ex.map(_correr, T):
            out[clave] = {k: v for k, v in m.items() if k != "atribucion"}
            print(f"{clave:42s} tasa {m['cagr']:7.1%}  caída pes {m['dd_pesimista']:6.1%}  estricta {m['dd_estricta']:6.1%}  "
                  f"comisiones {m['comisiones']:8.0f}  funding {m['funding']:7.0f}", flush=True)
    rev = RAIZ / "resultados" / "revalidacion"
    for n in ("20", "30"):
        p = rev / f"n{n}_OOS_B.json"
        if p.exists():
            d = json.loads(p.read_text())
            out[f"D_n{n}_pesos_congelados"] = {k: d[k] for k in ("cagr", "dd_optimista", "dd_pesimista", "dd_estricta",
                                                                  "comisiones", "funding")}
    (RES / "descomposicion.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=float))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k in ("perp", "spot", "top50", "resumen", "universo", "xbt"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--procesos", type=int, default=2)
    main(ap.parse_args())
