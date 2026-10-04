"""Tablas de AUDITORIA3.md generadas desde las salidas (ningún número se copia a mano).

    python -m validacion.tablas_auditoria3 > /tmp/tablas.md
"""
import json
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
RES = RAIZ / "resultados"
REV = RES / "revalidacion"
A3 = RES / "auditoria3"
NIV = ["10", "20", "25", "30"]
# 1.2 en la prueba (informe del auditor, sección 2, arranque nuevo en 2024): caída estricta y años
ANTES_ESTRICTA = {"10": -0.0599, "20": -0.0937, "25": -0.1171, "30": -0.1487}
ANTES_AÑOS = {"10": (0.0926, 0.0867, 0.0521), "20": (0.1969, 0.1824, 0.0708), "25": (0.3465, 0.2072, 0.1243),
              "30": (0.4195, 0.2815, 0.1003)}


def pc(x, d=1):
    return "—" if x is None else f"{x * 100:.{d}f} %".replace(".", ",").replace("-", "−")


def num(x, d=2):
    """Número en formato español: 1.200,09 y signo menos tipográfico."""
    if x is None:
        return "—"
    t = f"{abs(x):,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("−" if x < 0 else "") + t


def fx(x, d=2):
    return num(x, d)


def j(p):
    return json.loads(p.read_text()) if p.exists() else None


def revalidacion():
    r = j(REV / "revalidacion.json")
    out = ["**Corrida B (3000 USDT, contratos reales). Antes = motor 1.2 (c08d856); después = motor 1.3.**", "",
           "| Nivel | Tasa IS antes → después | p95 IS antes → después | Tasa prueba antes → después | Caída pesimista prueba "
           "antes → después | Caída estricta prueba antes → después | Calmar prueba antes → después | Sharpe prueba | C1 | C2 | C3 |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for n in NIV:
        v = r["niveles"].get(n)
        if not v:
            continue
        a, i, o, c = v["antes"], v["IS_B"], v["OOS_B"], v["criterios"]
        ok = lambda x: "✔" if x else "✘"
        out.append(f"| {n} % | {pc(a['IS_B']['cagr'])} → {pc(i['cagr'])} | {pc(a['IS_B']['p95'])} → {pc(i['p95'])} | "
                   f"{pc(a['OOS_B']['cagr'])} → {pc(o['cagr'])} | {pc(a['OOS_B']['dd_pesimista'])} → {pc(o['dd_pesimista'])} | "
                   f"{pc(ANTES_ESTRICTA[n])} → {pc(o['dd_estricta'])} | {fx(a['OOS_B']['calmar'])} → {fx(o['calmar'])} | "
                   f"{fx(o['sharpe'])} | {ok(c['C1']['ok'])} | {ok(c['C2']['ok'])} | {ok(c['C3']['ok'])} |")
    out += ["", "C2 compara la caída pesimista de la prueba con el p95 de IS del mismo motor. También se cumple con el p95 "
            "congelado y con la caída estricta en los cuatro niveles." if all(
                v["criterios"]["C2"]["ok_con_limite_congelado"] and v["criterios"]["C2"]["estricta_ok"]
                for v in r["niveles"].values()) else "Ver C2 con el p95 congelado y con la caída estricta en revalidacion.json.", "",
            "**Por año** (prueba con arranque nuevo en 2024; IS 2020–2023) y exposición máxima:", ""]
    out += ["| Nivel | Prueba 2024 antes → después | 2025 antes → después | 2026 (a sep) antes → después | "
            "IS 2020 | 2021 | 2022 | 2023 | Nocional máx. / patrimonio (IS · prueba) | Exposición máx. con BTC del margen (IS · prueba) |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for n in NIV:
        v = r["niveles"].get(n)
        if not v:
            continue
        po, pi = v["por_año_OOS"], v["por_año_IS"]
        b = ANTES_AÑOS[n]
        out.append(f"| {n} % | {pc(b[0])} → {pc(po.get('2024'))} | {pc(b[1])} → {pc(po.get('2025'))} | "
                   f"{pc(b[2])} → {pc(po.get('2026'))} | {pc(pi.get('2020'))} | {pc(pi.get('2021'))} | {pc(pi.get('2022'))} | "
                   f"{pc(pi.get('2023'))} | {fx(v['IS_B']['nocional_max'], 3)}× · {fx(v['OOS_B']['nocional_max'], 3)}× | "
                   f"{fx(v['IS_B']['exposicion_max'])}× · {fx(v['OOS_B']['exposicion_max'])}× |")
    if any("IS_A" in v for v in r["niveles"].values()):
        out += ["", "| Nivel | Corrida A IS: tasa · p95 | Corrida A prueba: tasa · caída pesimista · estricta |", "|---|---|---|"]
        for n in NIV:
            v = r["niveles"].get(n, {})
            if "IS_A" in v and "OOS_A" in v:
                out.append(f"| {n} % | {pc(v['IS_A']['cagr'])} · {pc(v['IS_A']['p95'])} | {pc(v['OOS_A']['cagr'])} · "
                           f"{pc(v['OOS_A']['dd_pesimista'])} · {pc(v['OOS_A']['dd_estricta'])} |")
    comp = r.get("comparaciones_OOS_B", {})
    if comp:
        out += ["", "**Referencias en la prueba (corrida B)**", "", "| Caso | Tasa | Caída | Calmar |", "|---|---|---|---|"]
        nombres = dict(btc_tendencia="BTC tendencia (SMA 200, perpetuo 1×)", btc_mantener="BTC mantener (perpetuo 1×, funding)",
                       eth_mantener="ETH mantener (perpetuo 1×, funding)", igual_riesgo="Pesos iguales por riesgo (nivel 30)",
                       sin_balas="Nivel 30 sin 30 balas", sin_alts="Nivel 30 sin alts", btc_spot_mantener="BTC al contado (comprar y mantener)",
                       eth_spot_mantener="ETH al contado (comprar y mantener)")
        for k, x in comp.items():
            if isinstance(x, dict):
                dd = x.get("dd_pesimista", x.get("dd_optimista"))
                cal = x.get("calmar") if x.get("calmar") is not None else (x["cagr"] / abs(dd) if dd else None)
                out.append(f"| {nombres.get(k, k)} | {pc(x['cagr'])} | {pc(dd)} | {fx(cal)} |")
        out += ["", "Los pesos de «igual riesgo» salen de la volatilidad de IS de cada estrategia sola (corridas solas del motor "
                "1.2, que sólo fijan esa referencia); al contado, caída de cierres de 4 h."]
    iv = {n: v.get("intervalo_ventaja_vs_btc_tendencia") for n, v in r["niveles"].items()}
    if any(isinstance(x, dict) for x in iv.values()):
        out += ["", "**Incertidumbre muestral de la ventaja contra BTC tendencia** (2000 remuestreos por bloques de los retornos "
                "diarios de la prueba, pareados; percentiles 5 / 50 / 95 de la tasa anual):", "",
                "| Nivel | Cartera | BTC tendencia | Diferencia | P(diferencia > 0) |", "|---|---|---|---|---|"]
        for n, x in iv.items():
            if isinstance(x, dict):
                f = lambda q: " / ".join(pc(v, 0) for v in q)
                out.append(f"| {n} % | {f(x['cartera'])} | {f(x['referencia'])} | {f(x['diferencia'])} | "
                           f"{pc(x['prob_diferencia_positiva'], 0)} |")
    op = {n: (v.get("IS_B_operable_kucoin"), v.get("OOS_B_operable_kucoin")) for n, v in r["niveles"].items()}
    if any(a or b for a, b in op.values()):
        out += ["", "**Variante operable en KuCoin** (informativa; símbolos con contrato del mismo ticker, desde su apertura):", "",
                "| Nivel | IS: tasa · caída · p95 | Prueba: tasa · caída · Calmar |", "|---|---|---|"]
        for n, (a, b) in op.items():
            if a or b:
                out.append(f"| {n} % | " + (f"{pc(a['cagr'])} · {pc(a['dd_pesimista'])} · {pc(a['p95'])}" if a else "—") + " | " +
                           (f"{pc(b['cagr'])} · {pc(b['dd_pesimista'])} · {fx(b['calmar'])}" if b else "—") + " |")
    return "\n".join(out)


def atribucion():
    r = j(REV / "n30_OOS_B.json")
    if not r:
        return "(pendiente)"
    a = r["atribucion"]
    out = ["| Parte | USDT | Nota |", "|---|---|---|"]
    for k, v in sorted(a["por_estrategia"].items(), key=lambda x: -x[1]):
        nota = ""
        if k in a.get("lotes_abiertos", {}):
            nota = f"incluye {num(a['lotes_abiertos'][k])} de lotes abiertos al último cierre"
        if k == "balas5":
            b = a["balas"]
            nota = (f"{b['campañas']} campañas, {b['liquidaciones']} liquidaciones; funding {num(b['funding'])}, "
                    f"comisiones {num(b['comisiones'])}")
        out.append(f"| {k} | {num(v)} | {nota} |")
    out.append(f"| **Total** | **{num(a['total'])}** | = patrimonio final − inicial; residuo de la principal "
               f"{a['residuo_principal']:.0e}, de balas {a.get('residuo_balas', 0):.0e} |")
    cb = r["campañas_balas"]
    out += ["", f"Costos de la prueba (nivel 30, B): funding de la cuenta principal {num(a['funding_principal'])} USDT y de "
            f"30 balas {num(a['balas']['funding'])}; comisiones de la principal {num(a['comisiones_principal'])} y de 30 balas "
            f"{num(a['balas']['comisiones'])} (incluye la conversión USDT ↔ BTC). Lotes cerrados de la principal, netos de "
            f"funding: {r.get('lotes_cerrados')}, {pc(r.get('ganadoras'), 0)} ganadores, factor de beneficio "
            f"{num(r.get('factor_beneficio'))}; campañas de 30 balas: {cb['n']}, {pc(cb['ganadoras'], 0)} ganadoras, factor "
            f"{num(cb['factor_beneficio'])}, peor {num(cb['peor'])} USDT."]
    return "\n".join(out)


def descomposicion():
    d = j(A3 / "descomposicion.json")
    if not d:
        return "(pendiente)"
    viejo = j(RES / "auditoria2" / "resumen.json") or {}
    filas = [("A", "1.0 declarada (investigación, pesos 1.0, spot)", "A_n{n}_declarado_1.0", "A_n{n}_declarado_1.0"),
             ("B", "motor, pesos 1.0, spot, sin funding, con costos", "B_n{n}_pesos10_spot", "B_n{n}_pesos10_spot"),
             ("C", "motor, pesos 1.0, perpetuos + funding, con costos", "C_n{n}_pesos10_perp", "C_n{n}_pesos10_perp"),
             ("E", "motor, pesos 1.0, perpetuos, sin costos ni funding (1.2: balas conservaba sus costos)",
              "E_n{n}_pesos10_perp_sin_costos", "E_n{n}_pesos10_perp_sin_costos_ni_funding"),
             ("D", "motor, pesos congelados (la evaluación oficial)", "D_n{n}_oos", "D_n{n}_pesos_congelados")]
    out = ["Mismo período (2024-01-01 → 2026-09-30), corrida B. Tasa anual y caída pesimista; motor 1.2 → motor 1.3.", "",
           "| Paso | Qué cambia | Nivel 20: tasa 1.2 → 1.3 | caída 1.3 | Nivel 30: tasa 1.2 → 1.3 | caída 1.3 |", "|---|---|---|---|---|---|"]
    for p, txt, k12, k13 in filas:
        celdas = []
        for n in ("20", "30"):
            a, b = viejo.get(k12.format(n=n)) or {}, d.get(k13.format(n=n)) or {}
            dd = b.get("dd_pesimista", b.get("dd_optimista"))
            celdas += [f"{pc(a.get('cagr'))} → {pc(b.get('cagr'))}", pc(dd)]
        out.append(f"| {p} | {txt} | " + " | ".join(celdas) + " |")
    c20, c30 = d.get("C_n20_pesos10_perp") or {}, d.get("C_n30_pesos10_perp") or {}
    out += ["", "A: la serie diaria que declaraba la 1.0 (caída de cierres diarios); el resto, caída pesimista del motor 1.3. "
            f"Costos del paso C en la prueba (arranque 3000 USDT): nivel 20, comisiones {num(c20.get('comisiones'), 0)} y funding "
            f"{num(c20.get('funding'), 0)} USDT; nivel 30, {num(c30.get('comisiones'), 0)} y {num(c30.get('funding'), 0)}. E − C "
            "no es «el costo»: sin costos ni funding cambian también las señales, los tamaños y la trayectoria; los pasos "
            "describen, no se suman."]
    return "\n".join(out)


if __name__ == "__main__":
    print("## revalidacion\n" + revalidacion())
    print("\n## atribucion\n" + atribucion())
    print("\n## descomposicion\n" + descomposicion())
