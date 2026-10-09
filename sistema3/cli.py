"""Línea de comandos:  docker compose exec sistema3 python -m sistema3.cli <comando>

  verificar            prueba Binance, CoinMarketCap, KuCoin (saldos spot y futuros) y Telegram
  plan                 calcula la decisión de la última vela cerrada SIN ejecutar nada
  estado               capital, modo y posiciones
  telegram-chat        muestra el chat_id de quien le escribió al bot
  aporte MONTO [nota]  registra un aporte (o retiro, con monto negativo) para que no cuente como rendimiento
  prueba-transferencia mueve 2 USDT de SPOT a FUTUROS y los devuelve
  alinear [--ejecutar]  adopta lo que hay en SPOT y lo lleva a la cartera que indica la señal de hoy
                       (sin --ejecutar sólo muestra el plan)
"""
import json
import sys
import time
import urllib.request

import pandas as pd

from . import config as C


def _servicio():
    from .principal import Servicio
    return Servicio()


def verificar():
    cfg = C.cargar()
    from .fuentes import Binance, top50_cmc
    b = Binance()
    ok = True
    try:
        d = b.diarias("BTC", 3)
        print(f"Binance spot OK · BTC cierre {d['c'].iloc[-1]:,.0f} ({d.index[-1].date()})")
        p = b.perp_4h("BTC", 3)
        print(f"Binance perpetuos OK · última vela 4h {p.index[-1]}")
    except Exception as e:
        ok = False; print("Binance ERROR:", e)
    try:
        t = top50_cmc(cfg.cmc_api_key)
        print(f"CoinMarketCap OK · top 50: {', '.join(s for _, s in t[:12])}…")
    except Exception as e:
        ok = False; print("CoinMarketCap ERROR:", e)
    if cfg.modo == "real":
        try:
            from .bolsa import KucoinReal
            k = KucoinReal(cfg.kucoin, cfg.apalancamiento_exchange)
            k.usdt_a_trading()
            s = k.saldos_spot()
            print("KuCoin spot OK · trade:", {a: round(v, 6) for a, v in s.items()})
            print(f"KuCoin futuros OK · patrimonio {k.patrimonio_futuros():.2f} USDT · posiciones {k.posiciones_futuros()}")
            print(f"Contrato SOL: {k.contrato('SOL')}  · mercado spot SOL: {k.mercado_spot('SOL')}")
        except Exception as e:
            ok = False; print("KuCoin ERROR:", e)
    from .telegram import Telegram
    tg = Telegram(cfg.telegram_token, cfg.telegram_chat)
    if tg.activo:
        tg.avisar("info", "Prueba de Sistema 3: Telegram OK")
        time.sleep(3)
        print("Telegram: mensaje de prueba enviado")
    else:
        print("Telegram: falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")
    return ok


def plan():
    s = _servicio()
    from .principal import ultimo_cierre
    t = ultimo_cierre()
    s.motor.actualizar_universo(t.normalize())
    A = s.motor.decidir_diaria(t.normalize())
    B = s.motor.decidir_4h(t)
    dec = dict(A=A, B=B)
    dec["acciones"] = s.motor.acciones_previstas(dec)
    print(f"Vela diaria {A['vela']} · vela 4h que abre {B['vela']}")
    print(s.motor.texto_decision(dec))
    print("\nRanking FR20 (ROC20 del par):")
    for x in A["ranking"]:
        print(f"  {x['sim']:8s} ROC20 {x['roc20']:+.2f}  ATR% {x['atrp'] * 100:.1f}  peso {x['peso'] * 100:.1f} %")
    print(f"Elegibles: {A['info']['n_elegibles']} · universo de cortos: {B['universo']}")
    if B["candidatas"]:
        print("Candidatas a corto:", ", ".join(f"{x['sim']} ({x['roc540']:+.0%})" for x in B["candidatas"]))


def estado():
    print(_servicio().texto_estado())


def telegram_chat():
    cfg = C.cargar()
    r = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{cfg.telegram_token}/getUpdates", timeout=20))
    vistos = {}
    for u in r.get("result", []):
        ch = (u.get("message") or {}).get("chat") or {}
        if ch.get("id"):
            vistos[ch["id"]] = ch.get("username") or ch.get("first_name")
    if not vistos:
        print("No hay mensajes: escribile cualquier cosa al bot y volvé a correr esto.")
    for i, n in vistos.items():
        print(f"TELEGRAM_CHAT_ID={i}   ({n})")


def aporte(monto, nota=""):
    s = _servicio()
    s.db.ejec("INSERT INTO flujos VALUES (?,?,?)", (int(time.time() * 1000), float(monto), nota))
    print(f"Registrado {float(monto):+.2f} USDT ({nota})")


def prueba_transferencia():
    s = _servicio()
    b = s.bolsa
    print("Futuros antes:", b.patrimonio_futuros())
    b.transferir(2, "futuros"); time.sleep(3)
    print("Futuros con 2 USDT:", b.patrimonio_futuros())
    b.transferir(min(2, b.libre_futuros()), "spot"); time.sleep(3)
    print("Futuros después:", b.patrimonio_futuros(), "· rutas:", b.ruta_ida and b.ruta_ida[1:], b.ruta_vuelta and b.ruta_vuelta[1:])


def alinear(ejecutar=False):
    s = _servicio()
    m = s.motor
    from .principal import ultimo_cierre
    hoy = ultimo_cierre().normalize()
    m.actualizar_universo(hoy)
    P = m.plan_alineacion(hoy)
    print(m.texto_alineacion(P))
    if not ejecutar:
        print("\n(Sólo el plan: no se envió ninguna orden. Para ejecutarlo: alinear --ejecutar)")
        return
    s.db.set("pausado", True)
    try:
        s.db.ejec("UPDATE decisiones SET estado='reemplazada' WHERE estado IN ('pendiente','nueva')")
        informe = m.ejecutar_alineacion(P)
        s.db.set("ultima_diaria", str(hoy.date()))
        m.registrar_patrimonio()
        print("\nEJECUTADO:\n" + "\n".join("• " + x for x in informe))
        s.tg.avisar("op", "Alineación inicial ejecutada:\n" + "\n".join("• " + x for x in informe))
        time.sleep(3)
        print("\n" + s.texto_estado())
    finally:
        s.db.set("pausado", False)


def main(argv=None):
    a = (argv or sys.argv)[1:]
    if not a:
        print(__doc__); return
    c = a[0]
    if c == "verificar":
        sys.exit(0 if verificar() else 1)
    elif c == "plan":
        plan()
    elif c == "estado":
        estado()
    elif c == "telegram-chat":
        telegram_chat()
    elif c == "aporte":
        aporte(a[1], " ".join(a[2:]))
    elif c == "alinear":
        alinear("--ejecutar" in a)
    elif c == "prueba-transferencia":
        prueba_transferencia()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
