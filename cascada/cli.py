"""Línea de comandos.  Dentro del contenedor:  docker compose exec cascada python -m cascada.cli <comando>

  verificar            prueba KuCoin (público y privado), CoinMarketCap y Telegram, y muestra tamaños de contrato
  estado               resumen como el de /estado
  aporte MONTO [nota]  registra un depósito (+) o retiro (−) para que no cuente como ganancia o caída
  reiniciar-papel      borra la base (sólo modo papel) para empezar de cero
  telegram-chat        muestra el chat_id de quien le escribió al bot (para TELEGRAM_CHAT_ID)
"""
import sys
import time

import pandas as pd

from . import config as C


def verificar():
    cfg = C.cargar()
    ok = True
    print(f"Modo {cfg.modo} · nivel {cfg.nivel} · techo {cfg.techo:.0%} · alerta {cfg.alerta_caida:.0%} · corte {cfg.corte_caida:.0%}")
    print("Pesos:", {k: v for k, v in cfg.pesos.items()}, "desactivadas:", cfg.desactivadas)
    from .bolsa import KucoinReal, Publico
    try:
        pub = Publico(); m = pub.mercados()
        print(f"✔ KuCoin público: {len(m)} futuros USDT. Contrato BTC = {m['BTC']['tam']} BTC (mín {m['BTC']['minimo']}),"
              f" ETH = {m['ETH']['tam']} ETH")
        p = pub.precios(["BTC", "ETH"])
        print(f"  precios BTC {p.get('BTC')} · ETH {p.get('ETH')} · 1 contrato BTC ≈ {m['BTC']['tam'] * p['BTC']:.0f} USDT")
    except Exception as e:
        ok = False; print("✘ KuCoin público:", e)
    if cfg.kucoin.get("apiKey"):
        try:
            b = KucoinReal(cfg.kucoin, pub)
            print(f"✔ KuCoin cuenta principal: patrimonio {b.patrimonio():.2f} USDT, posiciones {b.posiciones()}")
        except Exception as e:
            ok = False; print("✘ KuCoin cuenta principal:", e)
    else:
        print("· Sin claves de la cuenta principal (normal en modo papel)")
    if cfg.kucoin_balas.get("apiKey"):
        try:
            import ccxt
            ex = ccxt.kucoinfutures({**cfg.kucoin_balas, "enableRateLimit": True})
            bal = ex.fetch_balance({"currency": "BTC"})
            print(f"✔ KuCoin subcuenta balas: {bal['total'].get('BTC')} BTC en futuros")
        except Exception as e:
            ok = False; print("✘ KuCoin subcuenta balas:", e)
    if cfg.cmc_api_key:
        try:
            from .datos import top50_cmc
            t = top50_cmc(cfg.cmc_api_key)
            print(f"✔ CoinMarketCap: top 50 = {', '.join(s for _, s, _ in t[:12])}…")
        except Exception as e:
            ok = False; print("✘ CoinMarketCap:", e)
    else:
        ok = False; print("✘ Falta CMC_API_KEY")
    if cfg.telegram_token and cfg.telegram_chat:
        try:
            from .telegram import Telegram
            tg = Telegram(cfg.telegram_token, cfg.telegram_chat)
            tg._api("sendMessage", dict(chat_id=tg.chat, text="✅ Prueba de Cascada: Telegram funciona"))
            print("✔ Telegram: mensaje de prueba enviado")
        except Exception as e:
            ok = False; print("✘ Telegram:", e)
    else:
        print("· Telegram sin configurar")
    print("✔ Web con clave" if cfg.web_clave else "✘ Falta WEB_CLAVE")
    ok = ok and bool(cfg.web_clave)
    print("\nTODO BIEN" if ok else "\nHAY COSAS PARA CORREGIR")
    return ok


def main(argv=None):
    a = argv if argv is not None else sys.argv[1:]
    if not a or a[0] in ("-h", "--help", "ayuda"):
        print(__doc__); return
    cmd = a[0]
    if cmd == "verificar":
        sys.exit(0 if verificar() else 1)
    if cmd == "telegram-chat":
        import json, urllib.request
        tok = C.cargar().telegram_token
        if not tok:
            sys.exit("Primero poné TELEGRAM_TOKEN en .env")
        r = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=30))
        chats = {(u["message"]["chat"]["id"], u["message"]["chat"].get("first_name") or u["message"]["chat"].get("title"))
                 for u in r.get("result", []) if "message" in u}
        print("\n".join(f"chat_id {i}  ({n})" for i, n in chats) or "No hay mensajes: escribile cualquier cosa a tu bot y repetí.")
        return
    from .db import Base
    db = Base(C.ruta_base(C.cargar()))
    if cmd == "estado":
        from . import estadisticas as ES
        print(ES.texto_estado(db, C.cargar()))
    elif cmd == "aporte":
        monto = float(a[1]); nota = " ".join(a[2:])
        db.ejec("INSERT INTO flujos VALUES (?,?,?,?)", (int(time.time() * 1000), "total", monto, nota))
        mx = db.get("maximo_patrimonio")
        if mx is not None:
            db.set("maximo_patrimonio", mx + monto)
        print(f"Registrado {'aporte' if monto > 0 else 'retiro'} de {abs(monto):.2f} USDT")
    elif cmd == "reiniciar-papel":
        cfg = C.cargar()
        if cfg.modo != "papel":
            sys.exit("Sólo en modo papel.")
        if input("Borra toda la historia de papel. Escribí BORRAR: ") == "BORRAR":
            ruta = db.ruta; db.cx.close()
            for suf in ("", "-wal", "-shm"):
                p = C.Path(ruta + suf)
                if p.exists():
                    p.unlink()
            print("Base borrada. Reiniciá el servicio: docker compose restart cascada")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
