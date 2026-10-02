"""Riesgo: patrimonio y caída, y previsión de incidencias futuras (el tope y la conciliación están en el motor)."""
import time

import pandas as pd


def _ms(t):
    return int(pd.Timestamp(t).value // 10**6)


def nocional(lib, precios):
    return sum(abs(L["contratos"]) * L["tam_contrato"] * precios.get(L["simbolo"], L["precio_entrada"]) for L in lib.values())


def actualizar_patrimonio(db, t, E, E_main, E_bal, lib, precios, cfg):
    maximo = max(db.get("maximo_patrimonio", E), E)
    db.set("maximo_patrimonio", maximo)
    caida = E / maximo - 1 if maximo > 0 else 0.0
    n = nocional(lib, precios)
    ts = _ms(t)
    for cuenta, valor, no in (("total", E, n), ("principal", E_main, n), ("balas", E_bal, None)):
        db.ejec("INSERT OR REPLACE INTO patrimonio VALUES (?,?,?,?,?,?)", (ts, cuenta, valor, no, maximo if cuenta == "total" else None,
                                                                         caida if cuenta == "total" else None))
    return dict(caida=caida, alerta=-caida >= cfg.alerta_caida, corte=-caida >= cfg.corte_caida, nocional=n, maximo=maximo)


def previsiones(db, cfg, precios=None, funding=None, balas=None):
    """Incidencias posibles en los próximos ciclos, ordenadas por gravedad. Lo muestra la web y Telegram resume."""
    out = []
    precios = precios or {}
    maximo = db.get("maximo_patrimonio")
    u = db.get("ultimo_ciclo") or {}
    E = u.get("E")
    if maximo and E:
        caida = E / maximo - 1
        falta_alerta = cfg.alerta_caida + caida
        falta_corte = cfg.corte_caida + caida
        if falta_corte < 0.05:
            out.append(("alta", "corte_cerca", f"A {falta_corte:.1%} del corte por caída ({cfg.corte_caida:.0%})"))
        elif falta_alerta < 0.05:
            out.append(("media", "alerta_cerca", f"A {falta_alerta:.1%} de la alerta de caída ({cfg.alerta_caida:.0%})"))
    if u.get("ts") and time.time() - u["ts"] > 5 * 3600:
        out.append(("alta", "ciclo_atrasado", f"El último ciclo fue hace {(time.time() - u['ts']) / 3600:.1f} h"))
    lib = db.filas("SELECT * FROM lotes WHERE cerrado_ts IS NULL")
    for L in lib:
        px = precios.get(L["simbolo"])
        if px and L["stop"]:
            dist = (px - L["stop"]) / px * L["lado"]
            if dist < 0.02:
                out.append(("media", "stop_cerca", f"{L['estrategia']} {L['simbolo']}: precio a {dist:.1%} del stop"))
        if funding and L["simbolo"] in funding and funding[L["simbolo"]] is not None:
            f = funding[L["simbolo"]] * L["lado"]       # positivo = la posición paga
            if f > 0.0005:
                out.append(("media", "funding", f"{L['simbolo']}: funding {funding[L['simbolo']]:.3%} cada 8 h en contra de {L['estrategia']}"))
    if E and precios:
        n = sum(abs(L["contratos"]) * L["tam_contrato"] * precios.get(L["simbolo"], L["precio_entrada"]) for L in lib if L["cuenta"] == "principal")
        lim = (cfg.tope_nocional - cfg.pesos.get("balas5", 0)) * E
        if lim > 0 and n / lim > 0.9:
            out.append(("baja", "tope_cerca", f"Uso del capital {n / lim:.0%} del tope: las entradas de baja prioridad se achican"))
    bm = db.filas("SELECT mensaje FROM incidencias WHERE tipo='bajo_minimo' AND resuelta=0 ORDER BY id DESC LIMIT 1")
    if bm:
        out.append(("media", "bajo_minimo", bm[0]["mensaje"]))
    hoy = pd.Timestamp.now("UTC").tz_localize(None)
    dias = (6 - hoy.dayofweek) % 7
    if dias <= 1:
        out.append(("baja", "universo", "Cambio del top 50 el domingo: pueden entrar y salir candidatas de cortos y momentum"))
    if balas:
        if balas.get("dist_liq") is not None and balas["dist_liq"] < 0.20:
            out.append(("alta" if balas["dist_liq"] < 0.12 else "media", "balas_liq",
                        f"30 balas: liquidación a {balas['dist_liq']:.1%} del precio"))
        if balas.get("reserva_usada"):
            out.append(("media", "balas_reserva", "30 balas: la reserva ya entró en la campaña actual"))
    if db.get("liquidando"):
        out.append(("critica", "liquidando", "Cierre total en curso: quedan posiciones abiertas, se reintenta cada minuto"))
    if not db.get("conciliacion_ok", True):
        out.append(("alta", "conciliacion", "Libro y exchange no coinciden: no se abren lotes nuevos hasta corregir"))
    orden = {"critica": 0, "alta": 1, "media": 2, "baja": 3}
    out.sort(key=lambda x: orden[x[0]])
    db.set("previsiones", [dict(nivel=a, tipo=b, mensaje=c) for a, b, c in out])
    return out
