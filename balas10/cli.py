"""Línea de comandos:  docker compose exec balas10 python -m balas10.cli <comando>

  verificar                  prueba Binance, KuCoin (posición XBTUSDM y BTC de futuros) y Telegram
  plan                       señal de la última vela cerrada y lo que haría, SIN operar
  estado                     posición, liquidación, balas, reserva y señal
  adoptar --c1 0.25 --reserva 0.25 --c2-afuera 0.125 --balas 30 --desde 2026-10-01 [--reserva-inicial-puesta] [--ejecutar]
                             toma la campaña abierta en KuCoin (sin --ejecutar sólo muestra el plan)
  aprobar ID / rechazar ID   confirma o descarta la decisión pendiente (si no hay bot propio de Telegram)
  afuera X                   BTC del cargador 2 que siguen fuera del exchange
  telegram-chat              muestra el chat_id de quien le escribió al bot propio (TELEGRAM_TOKEN_BALAS)
"""
import argparse
import json
import sys
import urllib.request

import pandas as pd

from . import config as C


def _servicio():
    from .principal import Servicio
    return Servicio()


def verificar():
    cfg = C.cargar()
    from .fuentes import BinanceBTC
    f = BinanceBTC()
    c4, d = f.btc_4h(5), f.btc_1d(5)
    print(f"Binance OK · última vela 4h {c4.index[-1]} cierre {c4.iloc[-1]:,.0f} · último día {d.index[-1].date()}")
    if cfg.modo == "real":
        from .bolsa import KucoinInverso
        k = KucoinInverso(cfg.kucoin)
        print("KuCoin futuros · BTC:", k.cuenta())
        print("KuCoin XBTUSDM · posición:", k.posicion())
        print("KuCoin XBTUSDM · precio:", k.precio())
    from sistema3.telegram import Telegram
    tg = Telegram(cfg.telegram_token, cfg.telegram_chat)
    if tg.activo:
        tg.avisar("info", "Prueba de 30 balas 10x: Telegram OK")
        import time; time.sleep(3)
        print("Telegram: mensaje enviado" + ("" if cfg.telegram_escuchar else " (bot del Sistema 3, sólo avisos)"))


def plan():
    s = _servicio()
    from .principal import ultimo_cierre
    t = ultimo_cierre()
    sig = s.motor.senales(t)
    print(json.dumps({k: sig[k] for k in ("t", "dia", "cierre", "sma", "sobre", "sobre_previa", "rsi", "regimen",
                                            "levm", "momento", "ret90", "vol", "mayer", "entrada")}, indent=1, default=str))
    print(s.motor.texto_estado())
    st = s.motor.st
    if st["activo"]:
        salir = (st["rec"] and sig["sobre_previa"] and not sig["sobre"]) or (sig["dia"] and not sig["regimen"])
        print("Con esta vela:", "SALIDA" if salir else "sigue la campaña")
    else:
        print("Con esta vela:", "ENTRADA" if sig["entrada"] else "sin entrada")


def adoptar(a):
    s = _servicio()
    m = s.motor
    pos = m.bolsa.posicion()
    cuenta = m.bolsa.cuenta()
    if pos is None:
        sys.exit("No hay posición XBTUSDM abierta en KuCoin.")
    print(f"Posición: {int(pos['contratos'])} contratos · entrada {pos['entrada']:,.1f} · marca {pos['marca']:,.1f} · "
          f"liquidación {pos['liq']:,.1f} ({pos['marca'] / pos['liq'] - 1:+.1%}) · margen {pos['margen']:.4f} BTC · modo {pos['modo']}")
    print(f"Cuenta de futuros: equity {cuenta['equity']:.4f} BTC · libre {cuenta['libre']:.4f} BTC")
    p = m.p
    falta = a.reserva * (0 if a.reserva_inicial_puesta else 1)
    print(f"Capital de la campaña: cargador 1 {a.c1} + reserva {a.reserva} + cargador 2 {a.c2_afuera} (afuera) = "
          f"{a.c1 + a.reserva + a.c2_afuera:.4f} BTC · balas usadas {a.balas}/{p['B1']}")
    if cuenta["libre"] < falta - 1e-6:
        print(f"ATENCIÓN: la reserva ({falta} BTC) tiene que estar libre en FUTUROS y hay {cuenta['libre']:.4f}.")
    if not a.reserva_inicial_puesta:
        q = a.reserva * p["pref"]
        m_nuevo = pos["margen"] + q
        liq = pos["contratos"] * 1.007 / (m_nuevo + pos["contratos"] / pos["entrada"])
        print(f"Al adoptar entra la mitad de la reserva como margen: +{q:.4f} BTC → margen {m_nuevo:.4f}, "
              f"liquidación ≈ {liq:,.0f} ({pos['marca'] / liq - 1:+.1%} desde la marca).")
    if not a.ejecutar:
        print("\nPlan solamente. Para adoptar: agregar --ejecutar")
        return
    st, _ = m.adoptar(a.c1, a.reserva, a.c2_afuera, a.balas, a.desde, reserva_inicial_puesta=a.reserva_inicial_puesta)
    print(f"Adoptada. Cruzó sobre la SMA{p['ma']} desde {a.desde}: {'sí' if st['rec'] else 'no'}")
    inf = m.control()
    print("\n".join(inf) if inf else "Sin aportes de margen por ahora.")
    print(m.texto_estado())


def decidir(did, si):
    s = _servicio()
    p = s.motor.pendiente()
    if not p or p["id"] != did:
        sys.exit(f"No hay una decisión pendiente #{did}" + (f" (la pendiente es #{p['id']})" if p else ""))
    if not si:
        s.motor.marcar(did, "rechazada"); print("Descartada."); return
    s.motor.marcar(did, "aprobada")
    s._ejecutar(did, p["datos"])
    print(s.motor.decision(did)["resultado"])


def telegram_chat():
    cfg = C.cargar()
    if not cfg.telegram_escuchar:
        sys.exit("Falta TELEGRAM_TOKEN_BALAS en .env (un bot propio para 30 balas).")
    with urllib.request.urlopen(f"https://api.telegram.org/bot{cfg.telegram_token}/getUpdates", timeout=20) as r:
        d = json.load(r)
    for u in d.get("result", []):
        m = u.get("message") or {}
        print(m.get("chat", {}).get("id"), m.get("from", {}).get("username"), m.get("text"))


def main():
    ap = argparse.ArgumentParser(prog="balas10.cli")
    sub = ap.add_subparsers(dest="cmd")
    for c in ("verificar", "plan", "estado", "telegram-chat"):
        sub.add_parser(c)
    ad = sub.add_parser("adoptar")
    ad.add_argument("--c1", type=float, required=True)
    ad.add_argument("--reserva", type=float, required=True)
    ad.add_argument("--c2-afuera", type=float, required=True)
    ad.add_argument("--balas", type=int, required=True)
    ad.add_argument("--desde", required=True, help="fecha (UTC) en que empezó la campaña, p. ej. 2026-10-01")
    ad.add_argument("--reserva-inicial-puesta", action="store_true")
    ad.add_argument("--ejecutar", action="store_true")
    for c in ("aprobar", "rechazar"):
        x = sub.add_parser(c); x.add_argument("id")
    af = sub.add_parser("afuera"); af.add_argument("btc", type=float)
    a = ap.parse_args()
    if a.cmd == "verificar":
        verificar()
    elif a.cmd == "plan":
        plan()
    elif a.cmd == "estado":
        print(_servicio().motor.texto_estado())
    elif a.cmd == "adoptar":
        adoptar(a)
    elif a.cmd in ("aprobar", "rechazar"):
        decidir(a.id, a.cmd == "aprobar")
    elif a.cmd == "afuera":
        print(_servicio().comando("afuera", [str(a.btc)]))
    elif a.cmd == "telegram-chat":
        telegram_chat()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
