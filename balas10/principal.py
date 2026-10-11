"""Servicio de 30 balas 10x: ciclo al cierre de cada vela de 4 h, control de margen cada 15 minutos, confirmaciones y
comandos por Telegram (bot propio) y un /salud local para el healthcheck de Docker.

    python -m balas10.principal
"""
import logging
import logging.handlers
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pandas as pd

from sistema3.db import Base
from sistema3.telegram import Telegram

from . import config as C
from .bolsa import KucoinInverso, PapelInverso
from .fuentes import BinanceBTC
from .motor import Motor, ahora, btc

VERSION = "1.0.0"
log = logging.getLogger("balas10")
CONTROL_MIN = 15

AYUDA = """30 balas 10x — comandos:
/estado – posición, liquidación, balas, reserva y señal
/pendiente – la decisión que espera confirmación
/si ID – ejecutar la decisión ID
/no ID – descartar la decisión ID
/afuera X – BTC del cargador 3 que siguen fuera del exchange (p. ej. /afuera 0.125)
/pausar – no abre, no compra y no cierra (los aportes de margen siguen)
/reanudar – vuelve a operar
/ayuda – esta lista"""


def configurar_log():
    C.DATOS.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    h = logging.handlers.RotatingFileHandler(C.DATOS / "balas10.log", maxBytes=5_000_000, backupCount=5)
    h.setFormatter(fmt)
    s = logging.StreamHandler(); s.setFormatter(fmt)
    root = logging.getLogger(); root.setLevel(logging.INFO); root.addHandler(h); root.addHandler(s)


def ultimo_cierre(t=None):
    return pd.Timestamp(t or ahora()).floor("4h")


class Servicio:
    def __init__(self, cfg=None, bolsa=None, fuente=None, ruta_db=None, telegram=None):
        self.cfg = cfg = cfg or C.cargar()
        self.db = Base(ruta_db or C.ruta_base(cfg))
        self.tg = telegram or Telegram(cfg.telegram_token, cfg.telegram_chat)
        self.fuente = fuente or BinanceBTC()
        if bolsa is None:
            if cfg.modo == "real":
                if not cfg.kucoin.get("apiKey"):
                    raise SystemExit("Modo real sin claves de KuCoin en .env")
                bolsa = KucoinInverso(cfg.kucoin)
            else:
                bolsa = PapelInverso(self.db, lambda: float(self.fuente.btc_4h(2).iloc[-1]), btc=cfg.capital_papel_btc)
        self.bolsa = bolsa
        self.motor = Motor(cfg, self.db, bolsa, self.fuente, avisar=self.tg.avisar, p=cfg.parametros)
        self.cerrojo = threading.RLock()
        self.db.set("version", VERSION)

    def hora_local(self, ms_):
        t = pd.to_datetime(ms_, unit="ms").tz_localize("UTC").tz_convert(self.cfg.zona_horaria)
        return t.strftime("%d/%m %H:%M") + " hora local"

    # ------------------------------------------------------------------------------------------ ciclo
    def ciclo(self, t):
        t = pd.Timestamp(t)
        with self.cerrojo:
            informe, dec = self.motor.ciclo(t)
            self.db.set("ultimo_ciclo", dict(ts=time.time(), vela=str(t)))
            g = self.motor.st.get("ultima_senal") or {}
            log.info("ciclo %s · señal %s · decisión %s · %s", t, {k: g.get(k) for k in ("regimen", "rsi", "sobre", "levm", "entrada")},
                     dec and dec["tipo"], informe)
            if informe:
                self.tg.avisar("op", "30 balas · vela " + f"{t:%d/%m %H:%M} UTC\n" + "\n".join("• " + x for x in informe))
            if not dec:
                return
            did = self.motor.nueva_decision(t, dec)
            texto = self.motor.texto_decision(dec)
            if self.db.get("pausado"):
                self.motor.marcar(did, "pausado")
                self.tg.avisar("alta", f"30 balas · decisión #{did} (en pausa, no se ejecuta)\n{texto}")
            elif self.cfg.confirmar and self.cfg.modo == "real":
                self.motor.marcar(did, "pendiente")
                v = self.motor.decision(did)
                como = f"/si {did} para ejecutar o /no {did}" if self.cfg.telegram_escuchar else \
                    f"en el servidor: python -m balas10.cli aprobar {did}"
                self.tg.avisar("pregunta", f"30 balas · decisión #{did} · vela {t:%d/%m %H:%M} UTC\n{texto}\n\n"
                                           f"Responder {como} (vence {self.hora_local(v['vence_ts'])}).")
            else:
                self._ejecutar(did, dec)

    def _ejecutar(self, did, dec):
        try:
            informe = self.motor.ejecutar(dec)
            self.motor.marcar(did, "ejecutada", "\n".join(informe))
            self.tg.avisar("op", f"30 balas · #{did} ejecutada:\n" + "\n".join("• " + x for x in informe))
        except Exception as e:
            log.exception("ejecución #%s", did)
            self.motor.marcar(did, "error", str(e)[:500])
            self.db.incidencia("critica", "ejecucion", f"#{did}: {str(e)[:300]}")
            self.tg.avisar("critica", f"30 balas · error al ejecutar #{did}: {str(e)[:300]}\nRevisar la cuenta en KuCoin.")

    def ciclo_con_reintentos(self, t):
        ultimo = None
        for i, espera in enumerate((0, 60, 120, 300)):
            time.sleep(espera)
            try:
                self.ciclo(t)
                return True
            except Exception as e:
                ultimo = e
                log.warning("ciclo %s intento %d: %s", t, i + 1, e)
        self.db.set("ultimo_ciclo", dict(ts=time.time(), vela=str(t), error=str(ultimo)[:300]))
        self.db.incidencia("critica", "ciclo", f"Ciclo {t}: {str(ultimo)[:300]}")
        self.tg.avisar("critica", f"30 balas · no pude decidir la vela {t}: {str(ultimo)[:300]}")
        log.error("ciclo %s: %s", t, traceback.format_exception(ultimo))
        return False

    def control(self):
        with self.cerrojo:
            try:
                self.motor.pendiente()
                informe = self.motor.control()
                if informe:
                    self.tg.avisar("op", "30 balas · control\n" + "\n".join("• " + x for x in informe))
                v = self.motor.valuar()
                self.db.ejec("INSERT OR REPLACE INTO patrimonio VALUES (?,?,?,?,?,?,?)",
                             (int(time.time() * 1000), v["total_usd"] or 0, 0.0, v["total_btc"], v["precio"],
                              (v["pos"]["contratos"] / v["total_usd"]) if v["pos"] and v["total_usd"] else 0.0, 0.0))
                self.db.set("latido_control", time.time())
            except Exception as e:
                log.warning("control: %s", e)
                self.db.incidencia("media", "control", str(e)[:300])

    # ------------------------------------------------------------------------------------------ comandos
    def comando(self, nombre, args):
        with self.cerrojo:
            if nombre in ("ayuda", "start", "help"):
                return AYUDA
            if nombre == "estado":
                p = self.motor.pendiente()
                return self.motor.texto_estado() + (f"\nPendiente #{p['id']}: {p['resumen']}" if p else "") + \
                    (" · EN PAUSA" if self.db.get("pausado") else "")
            if nombre == "pendiente":
                p = self.motor.pendiente()
                return f"#{p['id']} (vence {self.hora_local(p['vence_ts'])})\n{p['resumen']}" if p else "No hay decisiones pendientes."
            if nombre in ("si", "sí", "no"):
                p = self.motor.pendiente()
                if not p:
                    return "No hay decisiones pendientes."
                if not args or args[0].lstrip("#") != p["id"]:
                    return f"La pendiente es #{p['id']}. Responder /{nombre} {p['id']}."
                if nombre == "no":
                    self.motor.marcar(p["id"], "rechazada")
                    return f"#{p['id']} descartada. No se envió ninguna orden."
                self.motor.marcar(p["id"], "aprobada")
                threading.Thread(target=self._aprobada, args=(p,), daemon=True).start()
                return f"#{p['id']} aprobada: ejecutando…"
            if nombre == "afuera":
                if not args:
                    return f"Afuera declarados: {btc(self.motor.st['afuera'])} BTC. Uso: /afuera 0.125"
                x = float(args[0].replace(",", "."))
                s = self.motor.st; s["afuera"] = max(x, 0.0); self.motor.guardar(s)
                return f"Afuera: {btc(s['afuera'])} BTC."
            if nombre == "pausar":
                self.db.set("pausado", True)
                return "En pausa: no abre, no compra contratos ni cierra. Los aportes de margen siguen funcionando."
            if nombre == "reanudar":
                self.db.set("pausado", False)
                return "Reanudado."
            return "Comando desconocido. /ayuda"

    def _aprobada(self, p):
        with self.cerrojo:
            self._ejecutar(p["id"], p["datos"])

    def atender(self):
        while not self.tg.comandos.empty():
            nombre, args = self.tg.comandos.get()
            try:
                self.tg.avisar("info", self.comando(nombre, args))
            except Exception as e:
                self.tg.avisar("alta", f"Error en /{nombre}: {e}")

    # ------------------------------------------------------------------------------------------ salud y bucle
    def salud(self):
        servicio = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                lat = servicio.db.get("latido") or 0
                ok = time.time() - lat < 300
                self.send_response(200 if ok else 503); self.end_headers()
                self.wfile.write(b"ok" if ok else b"sin latido")

        srv = ThreadingHTTPServer(("127.0.0.1", self.cfg.puerto_salud), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    def correr(self):
        self.salud()
        if self.cfg.telegram_escuchar:
            self.tg.escuchar()
        self.tg.avisar("info", f"30 balas 10x v{VERSION} en marcha · modo {self.cfg.modo}"
                               + (" · con confirmación" if self.cfg.confirmar else "")
                               + ("" if self.cfg.telegram_escuchar else " · sin bot propio: confirmar con la línea de comandos"))
        hecho = self.db.get("ultimo_ciclo") or {}
        pendiente = ultimo_cierre()
        if hecho.get("vela") == str(pendiente) or ahora() - pendiente > pd.Timedelta(hours=3):
            pendiente = None
        ultimo_control = 0.0
        while True:
            self.db.set("latido", time.time())
            self.atender()
            t = ultimo_cierre()
            listo = ahora() >= t + pd.Timedelta(seconds=self.cfg.espera_cierre_s)
            if pendiente is not None and listo:
                self.ciclo_con_reintentos(pendiente); pendiente = None
            elif listo and (self.db.get("ultimo_ciclo") or {}).get("vela") != str(t):
                self.ciclo_con_reintentos(t)
            if time.time() - ultimo_control > CONTROL_MIN * 60:
                self.control(); ultimo_control = time.time()
            time.sleep(2)


def main():
    configurar_log()
    Servicio().correr()


if __name__ == "__main__":
    main()
