"""Línea de comandos.  Dentro del contenedor:  docker compose exec cascada python -m cascada.cli <comando>

  verificar            prueba KuCoin (público y privado), CoinMarketCap y Telegram, y muestra tamaños de contrato
  estado               resumen como el de /estado
  aporte MONTO [nota]  registra un depósito (+) o retiro (−) para que no cuente como ganancia o caída
  volumen [BASE …]     volumen diario del perpetuo que usa el filtro de liquidez de c40 (y de qué fuente sale)
  reiniciar-papel      borra la base (sólo modo papel) para empezar de cero
  balas-prueba [USDT]  prueba la ejecución real de 30 balas en la subcuenta con montos mínimos (12 USDT por defecto)
  transferencia-prueba [USDT]  prueba el reequilibrio real de 30 balas: manda USDT a la subcuenta y los trae (15 por defecto)
  x-prueba             comprueba las credenciales de X y publica un post de prueba
  telegram-chat        muestra el chat_id de quien le escribió al bot (para TELEGRAM_CHAT_ID)
"""
import sys
import time

import pandas as pd

from . import config as C


def verificar():
    cfg = C.cargar()
    ok = True
    print(f"Modo {cfg.modo} · nivel {cfg.nivel} · p95 de caída 2020–23 {cfg.p95_is or 0:.0%} · alerta {cfg.alerta_caida:.0%} · corte {cfg.corte_caida:.0%}")
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
    if cfg.x_publicar:
        try:
            from .x import X
            u = X(cfg.x).yo()
            print(f"✔ X: cuenta @{u['username']} (publica operaciones: {cfg.x_operaciones}, resumen diario: {cfg.x_resumen_diario})")
        except Exception as e:
            ok = False; print("✘ X:", getattr(e, "read", lambda: b"")().decode(errors="ignore")[:200] or e)
    else:
        print("· X apagado (x_publicar: false)")
    print("✔ Web con clave" if cfg.web_clave else "✘ Falta WEB_CLAVE")
    ok = ok and bool(cfg.web_clave)
    print("\nTODO BIEN" if ok else "\nHAY COSAS PARA CORREGIR")
    return ok


def prueba_balas(usdt=12.0, todo_si=False):
    """Prueba guiada de la ejecución real de 30 balas con montos mínimos. Se detiene antes de cada paso."""
    import json
    import logging
    logging.basicConfig(level=logging.INFO, format="   · %(message)s")
    cfg = C.cargar()
    if not cfg.kucoin_balas.get("apiKey"):
        sys.exit("Faltan KUCOIN_BALAS_KEY / KUCOIN_BALAS_SECRET / KUCOIN_BALAS_PASSPHRASE en .env")
    from .balas_real import EjecutorRealBalas
    from .balas import P
    ej = EjecutorRealBalas(cfg.kucoin_balas)
    reg = []

    def ver(titulo, x):
        txt = json.dumps(x, default=str, indent=1, ensure_ascii=False)
        print(f"\n== {titulo}\n{txt}")
        reg.append((titulo, x))

    def paso(texto):
        print(f"\n>>> {texto}")
        if todo_si:
            return True
        r = input("    ¿Sigo? (s = sí / n = cortar): ").strip().lower()
        if r != "s":
            print("Cortado. Si quedó algo abierto, corré de nuevo y elegí sólo el cierre, o cerralo a mano en KuCoin.")
            return False
        return True

    def fallo(e):
        print(f"\n!!! FALLÓ: {e}")
        reg.append(("FALLO", str(e)))
        resumen()
        sys.exit(1)

    def resumen():
        print("\n================ RESUMEN PARA MANDAR (no contiene claves) ================")
        for t, x in reg:
            print(f"- {t}: {json.dumps(x, default=str, ensure_ascii=False)[:600]}")
        print("========================================================================")

    print(f"Prueba de 30 balas en la subcuenta. Usa unos {usdt:.0f} USDT y deja todo en USDT al final.")
    try:
        ver("Saldos iniciales", ej.saldos()); ver("Posición inicial", ej.posicion())
        ver("Estado que lee el sistema (USDT, BTC spot, margen BTC realizado, contratos, entrada)", ej.estado())
    except Exception as e:
        fallo(f"no pude leer la subcuenta (claves, permisos o IP): {e}")
    try:
        if paso("1/7 Si hay USDT en la cuenta principal (main) de la subcuenta, pasarlo a trading"):
            ver("USDT movido main→trade", ej.usdt_a_trading())
        else:
            return
        if not paso(f"2/7 Comprar BTC por {usdt * 0.7:.2f} USDT y pasarlo a futuros (margen inicial)"):
            return
        btc = ej.aportar_margen(usdt * 0.7)
        ver("BTC comprado y transferido", dict(btc=btc, ruta=str(ej.ruta_ida[1:]) if ej.ruta_ida else None))
        ver("Saldos", ej.saldos())
        if not paso("3/7 Poner margen cruzado en XBTUSDM y abrir 10 contratos (10 USD) a mercado"):
            return
        ver("Modo de margen", ej.margen_cruzado())
        f = ej.contratos(10, "buy")
        ver("Orden de apertura", dict(estado=f.get("status"), precio=f.get("average"), contratos=f.get("filled")))
        time.sleep(2)
        pos = ej.posicion(); ver("Posición", pos)
        if pos:
            px = float(pos.get("entrada") or f.get("average") or 0)
            mb = ej.btc_futuros_libre()
            b = ej.saldos().get("futuros_BTC", {})
            tot = b.get("total", 0) if isinstance(b, dict) else 0
            ntn = abs(pos["contratos"])
            liq_sis = ntn * (1 + P["MMR"]) / (tot + ntn / px) if px else None
            ver("Liquidación: KuCoin vs fórmula del sistema", dict(kucoin=pos.get("liquidacion"), sistema=liq_sis,
                                                                  margen_btc=tot, nota="con ~1,2x las dos deberían rondar la mitad del precio y parecerse entre sí"))
        if not paso(f"4/7 Recarga: comprar BTC por {usdt * 0.3:.2f} USDT, pasarlo a futuros y sumar 5 contratos"):
            return
        ej.recargar(usdt * 0.3, 5)
        time.sleep(2)
        ver("Posición tras recarga", ej.posicion()); ver("Saldos", ej.saldos())
        ver("Estado que lee el sistema", ej.estado())
        if not paso("5/7 Cerrar: vender todos los contratos, pasar el BTC a spot y venderlo por USDT"):
            return
        ej.cerrar()
        time.sleep(3)
        ver("Posición final (debe ser null)", ej.posicion())
        ver("6/7 Saldos finales", ej.saldos()); ver("Estado que lee el sistema (debe quedar sin contratos ni BTC)", ej.estado())
        print("\n7/7 Listo. Revisá en KuCoin → Órdenes que estén las operaciones y que no quede posición abierta.")
    except Exception as e:
        fallo(e)
    resumen()


def prueba_transferencia(usdt=15.0, todo_si=False):
    """Prueba guiada del reequilibrio real de 30 balas: manda `usdt` de la principal a la subcuenta y los trae de vuelta."""
    import json
    import logging
    logging.basicConfig(level=logging.INFO, format="   · %(message)s")
    cfg = C.cargar()
    faltan = [n for n, v in (("KUCOIN_KEY/SECRET/PASSPHRASE (principal)", cfg.kucoin.get("apiKey")),
                             ("KUCOIN_BALAS_KEY/SECRET/PASSPHRASE (subcuenta)", cfg.kucoin_balas.get("apiKey")),
                             ("KUCOIN_BALAS_UID (UID de la subcuenta)", cfg.kucoin_balas_uid)) if not v]
    if faltan:
        sys.exit("Faltan en .env: " + ", ".join(faltan))
    from .balas_real import EjecutorRealBalas
    from .bolsa import KucoinReal, Publico
    from .subcuenta import TransferidorSubcuenta
    reg = []

    def ver(titulo, x):
        print(f"\n== {titulo}\n{json.dumps(x, default=str, indent=1, ensure_ascii=False)}")
        reg.append((titulo, x))

    def resumen():
        print("\n================ RESUMEN PARA MANDAR (no contiene claves) ================")
        for t, x in reg:
            print(f"- {t}: {json.dumps(x, default=str, ensure_ascii=False)[:600]}")
        print("========================================================================")

    def paso(texto):
        print(f"\n>>> {texto}")
        if todo_si:
            return True
        if input("    ¿Sigo? (s = sí / n = cortar): ").strip().lower() != "s":
            resumen(); return False
        return True

    try:
        pub = Publico()
        principal = KucoinReal(cfg.kucoin, pub)
        ej = EjecutorRealBalas(cfg.kucoin_balas)
        t = TransferidorSubcuenta(cfg.kucoin_balas_uid, ej, cred_principal=cfg.kucoin)
        px = pub.precios(["BTC"]).get("BTC")

        def saldos(titulo):
            u, b = t.saldo_sub(px)
            ver(titulo, dict(principal_patrimonio=round(principal.patrimonio(), 2), principal_libre=round(principal.usdt_libre(), 2),
                             subcuenta_usdt=round(u, 2), subcuenta_btc_usd=round(b, 2)))
        saldos("Saldos iniciales")
    except Exception as e:
        reg.append(("FALLO al leer", str(e))); resumen()
        sys.exit(f"No pude leer las cuentas (claves, permisos, IP o UID): {e}")
    try:
        if not paso(f"1/2 Mandar {usdt:.2f} USDT de futuros de la principal a la subcuenta"):
            return
        t.enviar(usdt); ver("Ruta principal → subcuenta", t.ruta_ida); time.sleep(3); saldos("Después de mandar")
        if not paso(f"2/2 Traer {usdt:.2f} USDT de la subcuenta a futuros de la principal"):
            return
        t.traer(usdt); ver("Ruta subcuenta → principal", t.ruta_vuelta); time.sleep(3); saldos("Después de traer")
        print("\nListo: las dos rutas funcionan. Para activarlo: `balas_transferir: true` en config/nivel.yaml.")
    except Exception as e:
        reg.append(("FALLO", str(e)))
        print(f"\n!!! FALLÓ: {e}")
    resumen()


def main(argv=None):
    a = argv if argv is not None else sys.argv[1:]
    if not a or a[0] in ("-h", "--help", "ayuda"):
        print(__doc__); return
    cmd = a[0]
    if cmd == "verificar":
        sys.exit(0 if verificar() else 1)
    if cmd == "balas-prueba":
        prueba_balas(float(a[1]) if len(a) > 1 else 12.0, "--si" in a)
        return
    if cmd == "transferencia-prueba":
        prueba_transferencia(float(a[1]) if len(a) > 1 and not a[1].startswith("-") else 15.0, "--si" in a)
        return
    if cmd == "x-prueba":
        from .x import X
        cfg = C.cargar(); x = X(cfg.x)
        if not x.activo:
            sys.exit("Faltan X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN o X_ACCESS_SECRET en .env")
        try:
            u = x.yo(); print(f"Credenciales OK: @{u['username']}")
            r = x._post(f"Cascada: prueba de conexión ({time.strftime('%d/%m %H:%M', time.gmtime())} UTC)")
            print("Publicado:", r["data"]["id"], "— podés borrarlo desde X")
        except Exception as e:
            sys.exit(f"Error: {getattr(e, 'code', '')} {getattr(e, 'read', lambda: str(e).encode())().decode(errors='ignore')[:400]}")
        return
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
    elif cmd == "volumen":
        from .bolsa import Publico
        from .datos import VolumenPerp
        v = VolumenPerp(Publico())
        for b in (a[1:] or ["BTC", "ETH", "SOL", "PEPE", "HYPE"]):
            d = v.diario(b)
            if d is None or d.empty:
                print(f"{b:6s} sin volumen")
            else:
                print(f"{b:6s} {v.fuente.get(b):8s} mediana 30 d {d.tail(30).median() / 1e6:,.1f} M USDT · último día {d.index[-1]:%Y-%m-%d}")
        if v.error_binance:
            print("Binance falló:", v.error_binance)
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
