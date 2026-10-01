"""Asignador en cascada: reparte el nocional disponible por prioridad.

Cada estrategia pide peso × E × Σfrac de sus lotes. Los lotes fijos ya abiertos (cortos Aberration, c40, Soldados)
consumen primero su nocional actual y no se achican salvo que la cartera pase el tope; los lotes reescalables
(RSI(2), WR2) y los lotes nuevos reciben la fracción `g` que alcance con el presupuesto que queda.
"""


def asignar(E_total, presupuesto, pesos, prioridad, deseos, libro, precios, mercados):
    """
    deseos: estrategia -> lista de lotes deseados.
    libro: id de lote -> fila del libro (lotes abiertos), con contratos y tam_contrato.
    Devuelve (objetivos, resumen): objetivos = id -> nocional objetivo (USDT, sin signo; None = mantener);
    resumen = estrategia -> dict(pedido, concedido, g).
    """
    restante = presupuesto
    objetivos, resumen = {}, {}
    for est in prioridad:
        w = pesos.get(est, 0.0)
        lotes = deseos.get(est, [])
        fijo, flex = 0.0, 0.0
        for L in lotes:
            ab = libro.get(L["id"])
            if ab and not L.get("reescalable"):
                px = precios.get(L["simbolo"], ab["precio_entrada"])
                fijo += abs(ab["contratos"]) * ab["tam_contrato"] * px
            else:
                flex += L["frac"] * w * E_total
        pedido = fijo + flex
        disponible = max(restante - fijo, 0.0)
        g = min(1.0, disponible / flex) if flex > 0 else 1.0
        if w <= 0:
            g = 0.0
        for L in lotes:
            ab = libro.get(L["id"])
            if ab and not L.get("reescalable"):
                objetivos[L["id"]] = None
            else:
                objetivos[L["id"]] = L["frac"] * w * E_total * g
        conced = fijo + g * flex
        restante -= conced
        resumen[est] = dict(pedido=pedido, concedido=conced, g=g)
    return objetivos, resumen


def a_contratos(nocional, precio, mercado):
    """Contratos enteros (múltiplos del mínimo) que no superan el nocional."""
    if nocional is None or precio <= 0:
        return 0.0
    paso = mercado.get("minimo", 1) or 1
    c = nocional / (mercado["tam"] * precio)
    return float(int(c / paso) * paso)
