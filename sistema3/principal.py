"""Servicio del Sistema 3: ciclo al cierre de cada vela de 4 h (la de las 00:00 UTC incluye la decisión diaria),
control cada 15 minutos, confirmaciones y comandos por Telegram y la web.

    python -m sistema3.principal
"""
import logging
import logging.handlers
import queue
import threading
import time
import traceback

import pandas as pd

from . import config as C
from . import estadisticas as ES
from .bolsa import KucoinReal, Papel
from .db import Base
from .fuentes import Binance
from .motor import Motor, ahora, pct, usd
from .telegram import AYUDA, Telegram

VERSION = "1.0.0"
log = logging.getLogger("sistema3")
CUATRO_H = pd.Timedelta(hours=4)
CONTROL_MIN = 15


def configurar_log():
    C.DATOS.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    h = logging.handlers.RotatingFileHandler(C.DATOS / "sistema3.log", maxBytes=5_000_000, backupCount=5)
    h.setFormatter(fmt)
    s = logging.StreamHandler(); s.setFormatter(fmt)
    root = logging.getLogger(); root.setLevel(logging.INFO); root.addHandler(h); root.addHandler(s)


def ultimo_cierre(t=None):
    t = pd.Timestamp(t or ahora())
    return t.floor("4h")


class Servicio:
    def __init__(self, cfg=None, fuente=None, bolsa=None, ruta_db=None, telegram=None):
        self.cfg = cfg = cfg or C.cargar()
        self.db = Base(ruta_db or C.ruta_base(cfg))
        self.tg = telegram or Telegram(cfg.telegram_token, cfg.telegram_chat)
        self.fuente = fuente or Binance()
        if bolsa is None:
            if cfg.modo == "real":
                if not cfg.kucoin.get("apiKey"):
                    raise SystemExit("Modo real sin claves de KuCoin en .env")
                bolsa = KucoinReal(cfg.kucoin, cfg.apalancamiento_exchange)
            else:
                f = self.fuente

                def precio(s, tipo):
                    if tipo == "spot":
                        return f.precio_spot(s)
                    d = f.perp_4h(s, 2)
                    return float(d["c"].iloc[-1])
                bolsa = Papel(self.db, precio, capital=cfg.capital_papel)
        self.bolsa = bolsa
        self.motor = Motor(cfg, self.db, self.fuente, bolsa, avisar=self.tg.avisar)
        self.cerrojo = threading.RLock()
        self.cola_web = queue.Queue()
        self.db.set("version", VERSION)
        self.db.set("modo_cuenta", cfg.modo)
        if self.db.get("inicio") is None:
            self.db.set("inicio", time.time())

    # ------------------------------------------------------------------------------------------ ciclo
    def ciclo(self, t_cierre):
        t_cierre = pd.Timestamp(t_cierre)
        with self.cerrojo:
            dia0 = t_cierre.normalize()
            # la decisión diaria se toma en el ciclo de las 00:00; si falló, el primer ciclo siguiente del día la recupera
            diaria = self.db.get("ultima_diaria") != str(dia0.date())
            A = B = None
            if diaria:
                try:
                    self.motor.actualizar_universo(dia0)
                except Exception as e:
                    self.db.incidencia("alta", "universo", f"No pude bajar el top 50: {e}")
                    self.tg.avisar("alta", f"No pude bajar el top 50 de CoinMarketCap: {str(e)[:200]}. Sigo con el de la semana pasada.")
                A = self.motor.decidir_diaria(dia0)
                self.motor.registrar_modo(A)
            B = self.motor.decidir_4h(t_cierre)
            did, dec = self.motor.nueva_decision(t_cierre, A, B)
            if diaria:
                self.db.set("ultima_diaria", str(dia0.date()))
            self.db.set("ultimo_ciclo", dict(ts=time.time(), vela=str(t_cierre), decision=did))
            texto = self.motor.texto_decision(dec)
            log.info("ciclo %s #%s\n%s", t_cierre, did, texto)
            if not dec["acciones"]:
                self.motor.marcar(did, "sin operaciones")
                self._ejecutar(did, dec, silencioso=True)
            elif self.db.get("pausado"):
                self.motor.marcar(did, "pausado")
                self.tg.avisar("alta", f"Decisión #{did} (sistema en pausa, no se ejecuta)\n{texto}")
            elif self.cfg.confirmar and self.cfg.modo == "real":
                self.motor.marcar(did, "pendiente")
                v = self.motor.decision(did)
                vence = pd.to_datetime(v["vence_ts"], unit="ms").strftime("%d/%m %H:%M")
                self.tg.avisar("pregunta", f"Decisión #{did} · vela {t_cierre:%d/%m %H:%M} UTC\n{texto}\n\n"
                                           f"Responder /si {did} para ejecutar o /no {did} para descartar (vence {vence} UTC).")
            else:
                self._ejecutar(did, dec)
            if diaria:
                self.resumen_diario()

    def _ejecutar(self, did, dec, silencioso=False):
        try:
            informe = self.motor.ejecutar(dec)
            self.motor.marcar(did, "ejecutada", "\n".join(informe))
            if informe:
                self.tg.avisar("op", f"#{did} ejecutada:\n" + "\n".join("• " + x for x in informe))
            elif not silencioso:
                self.tg.avisar("info", f"#{did} ejecutada: no hizo falta ninguna orden")
        except Exception as e:
            log.exception("ejecución #%s", did)
            self.motor.marcar(did, "error", str(e)[:500])
            self.db.incidencia("critica", "ejecucion", f"#{did}: {str(e)[:300]}")
            self.tg.avisar("critica", f"Error al ejecutar #{did}: {str(e)[:300]}\nRevisar la cuenta en KuCoin.")
        try:
            self.motor.registrar_patrimonio()
        except Exception:
            log.exception("patrimonio")

    def ciclo_con_reintentos(self, t):
        for i, espera in enumerate((0, 60, 120, 300)):
            time.sleep(espera)
            try:
                self.ciclo(t)
                self.db.resolver("ciclo")
                return True
            except Exception as e:
                log.warning("ciclo %s intento %d: %s", t, i + 1, e)
                ultimo = e
        self.db.set("ultimo_ciclo", dict(ts=time.time(), vela=str(t), error=str(ultimo)[:300]))
        self.db.incidencia("critica", "ciclo", f"Ciclo {t}: {str(ultimo)[:300]}")
        self.tg.avisar("critica", f"No pude decidir la vela {t}: {str(ultimo)[:300]}")
        log.error("ciclo %s: %s", t, traceback.format_exception(ultimo))
        return False

    # ------------------------------------------------------------------------------------------ control y avisos
    def control(self):
        with self.cerrojo:
            try:
                self.motor.pendiente()          # marca como vencida la que pasó su plazo (y avisa)
                self.motor.registrar_patrimonio()
                self.motor.control_margen()
                self.db.set("latido_control", time.time())
            except Exception as e:
                log.warning("control: %s", e)
                self.db.incidencia("media", "control", str(e)[:300])

    def texto_estado(self):
        k = ES.kpis(self.db)
        v = self.motor.valuar()
        L = self.motor.libro
        lin = [f"Capital {usd(v['T'])} USDT (spot {usd(v['spot'])}, futuros {usd(v['futuros'])})",
               f"Modo del día: {self.db.get('modo_actual') or '—'}" + (" · CONFLICTO (mitad)" if L["conflicto"] else "")
               + (" · EN PAUSA" if self.db.get("pausado") else "")]
        if k.get("hay"):
            lin.append(f"Hoy {pct(k['hoy'])} · semana {pct(k['semana'])} · mes {pct(k['mes'])} · total {pct(k['total_pct'])} · caída {pct(k['caida'])}")
        pos = ES.posiciones(self.motor, v)
        if pos:
            lin += [f"• {p['parte']} {p['simbolo']}: {usd(abs(p['valor'] or 0))} USDT ({pct(p['resultado'])})" for p in pos]
        else:
            lin.append("Sin posiciones (todo en USDT)")
        return "\n".join(lin)

    def resumen_diario(self):
        try:
            self.tg.avisar("resumen", "Resumen diario\n" + self.texto_estado())
        except Exception as e:
            log.warning("resumen: %s", e)

    # ------------------------------------------------------------------------------------------ comandos
    def comando(self, nombre, args):
        with self.cerrojo:
            if nombre in ("ayuda", "start", "help"):
                return AYUDA
            if nombre == "estado":
                return self.texto_estado()
            if nombre == "pendiente":
                p = self.motor.pendiente()
                return f"#{p['id']} (vence {pd.to_datetime(p['vence_ts'], unit='ms'):%d/%m %H:%M} UTC)\n{p['resumen']}" if p else "No hay decisiones pendientes."
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
                threading.Thread(target=self._ejecutar_aprobada, args=(p,), daemon=True).start()
                return f"#{p['id']} aprobada: ejecutando…"
            if nombre == "pausar":
                self.db.set("pausado", True)
                return "En pausa: las decisiones se calculan pero no se ejecutan."
            if nombre == "reanudar":
                self.db.set("pausado", False)
                return "Reanudado."
            if nombre == "resolver":
                self.db.ejec("UPDATE incidencias SET resuelta=1 WHERE resuelta=0")
                return "Incidencias marcadas como resueltas."
            return "Comando desconocido. /ayuda"

    def _ejecutar_aprobada(self, p):
        with self.cerrojo:
            # las cantidades se recalculan con el libro y los saldos de ahora
            self._ejecutar(p["id"], p["datos"])

    def atender(self):
        while not self.tg.comandos.empty():
            nombre, args = self.tg.comandos.get()
            try:
                self.tg.avisar("info", self.comando(nombre, args))
            except Exception as e:
                self.tg.avisar("alta", f"Error en /{nombre}: {e}")
        while not self.cola_web.empty():
            nombre, args, resp = self.cola_web.get()
            try:
                resp.put(self.comando(nombre, args))
            except Exception as e:
                resp.put(f"Error: {e}")

    # ------------------------------------------------------------------------------------------ bucle
    def correr(self):
        from . import web
        web.arrancar(self)
        self.tg.escuchar()
        self.tg.avisar("info", f"Sistema 3 v{VERSION} en marcha · modo {self.cfg.modo} · nivel {self.cfg.nivel}"
                               + (" · con confirmación" if self.cfg.confirmar else ""))
        hecho = self.db.get("ultimo_ciclo") or {}
        pendiente_arranque = ultimo_cierre()
        if hecho.get("vela") == str(pendiente_arranque) or ahora() - pendiente_arranque > pd.Timedelta(hours=3):
            pendiente_arranque = None
        ultimo_control = 0.0
        while True:
            self.db.set("latido", time.time())
            self.atender()
            t = ultimo_cierre()
            listo = ahora() >= t + pd.Timedelta(seconds=self.cfg.espera_cierre_s)
            if pendiente_arranque is not None and listo:
                self.ciclo_con_reintentos(pendiente_arranque); pendiente_arranque = None
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
