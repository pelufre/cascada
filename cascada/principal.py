"""Servicio principal: ciclo de 4 h, controles cada 15 min, universo semanal, Telegram y web.

    python -m cascada.principal
"""
import logging
import logging.handlers
import os
import time
import traceback

import pandas as pd

from . import config as C
from . import estadisticas as ES
from . import riesgo
from .balas import Balas, EjecutorRealBalas
from .bolsa import KucoinReal, Papel, Publico
from .datos import Datos
from .db import Base
from .motor import Motor
from .telegram import AYUDA, Telegram

VERSION = "1.0.0"
log = logging.getLogger("cascada")
CUATRO_H = pd.Timedelta(hours=4)
ESPERA_CIERRE = 20          # segundos después del cierre de vela antes de decidir
CONTROL_MIN = 15            # minutos entre controles de patrimonio y riesgo


def _ahora():
    return pd.Timestamp.now("UTC").tz_localize(None)


def configurar_log():
    C.DATOS.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    h = logging.handlers.RotatingFileHandler(C.DATOS / "cascada.log", maxBytes=5_000_000, backupCount=5)
    h.setFormatter(fmt)
    s = logging.StreamHandler(); s.setFormatter(fmt)
    root = logging.getLogger(); root.setLevel(logging.INFO); root.addHandler(h); root.addHandler(s)


class Sistema:
    """Arma todas las piezas a partir de la configuración (lo usan el servicio y la línea de comandos)."""

    def __init__(self, cfg=None, avisar=None, publico=None, ruta_db=None):
        self.cfg = cfg = cfg or C.cargar()
        self.db = Base(ruta_db or C.ruta_base(cfg))
        self.tg = Telegram(cfg.telegram_token, cfg.telegram_chat)
        self.avisar = avisar or self.tg.avisar
        self.pub = publico or Publico()
        self.datos = Datos(self.db, self.pub)
        w_bal = cfg.pesos.get("balas5", 0.0)
        if cfg.modo == "real":
            if not cfg.kucoin.get("apiKey"):
                raise SystemExit("Modo real sin claves de KuCoin en .env")
            self.bolsa = KucoinReal(cfg.kucoin, self.pub, cfg.apalancamiento_exchange)
            real_balas = None
            if cfg.balas_real and cfg.kucoin_balas.get("apiKey"):
                real_balas = EjecutorRealBalas(cfg.kucoin_balas)
            elif w_bal > 0:
                cfg.pesos["balas5"] = 0.0          # sin subcuenta real, balas no participa
                if "balas5" not in cfg.desactivadas:
                    cfg.desactivadas.append("balas5")
            self.balas = Balas(self.db, cfg.capital_balas_real, real=real_balas, avisar=self.avisar) if real_balas else None
        else:
            cap = cfg.capital_papel
            self.bolsa = Papel(self.db, self.pub.mercados(), capital=cap * (1 - w_bal), comision=cfg.comision)
            self.balas = Balas(self.db, cap * w_bal, avisar=self.avisar) if w_bal > 0 else None
        if self.balas and self.db.get("balas_inicial") is None:
            self.db.set("balas_inicial", self.balas.st["W"])
        self.motor = Motor(cfg, self.db, self.datos, self.bolsa, self.pub, avisar=self.avisar,
                           balas_patrimonio=lambda: self.balas.patrimonio() if self.balas else 0.0)

    # ------------------------------------------------------------ piezas del ciclo
    def bases(self, t):
        mer = self.pub.mercados()
        lib = self.motor.libro()
        u = self.datos.universo(t)
        faltan = [s for s in u if s not in mer]
        if faltan:
            self.db.incidencia("baja", "universo_sin_mercado", "Del top 50 sin futuro USDT en KuCoin (no se operan): " + ", ".join(faltan), 24 * 7)
        return sorted({s for s in u if s in mer} | {"BTC", "ETH"} | {L["simbolo"] for L in lib.values()})

    def ciclo(self, t):
        cfg, db = self.cfg, self.db
        t0 = time.time()
        if t.hour == 0:
            self.pub.mercados(refrescar=True)
        try:
            if self.datos.actualizar_universo(cfg.cmc_api_key, t):
                self.avisar("info", "Top 50 de la semana actualizado")
            db.resolver("universo")
        except Exception as e:
            if db.incidencia("alta", "universo", f"No pude bajar el top 50 de CoinMarketCap: {e}"[:300]):
                self.avisar("alta", f"No pude bajar el top 50: {e}"[:300])
        bases = self.bases(t)
        errores = self.datos.actualizar(bases, hasta_ms=int(t.value // 10**6))
        if errores:
            db.incidencia("media", "velas", "Sin velas nuevas para: " + ", ".join(b for b, _ in errores)[:300])
        else:
            db.resolver("velas")
        precios = self.pub.precios(bases)
        db.set("precios", precios)
        # vela recién cerrada de cada símbolo con lote abierto (stops del modo papel)
        velas = {}
        for b in {L["simbolo"] for L in self.motor.libro().values()} | {"BTC"}:
            d = self.datos.v4(b, t, dias=2)
            if len(d) and d.index[-1] == t - CUATRO_H:
                velas[b] = (d.o.iat[-1], d.h.iat[-1], d.l.iat[-1], d.c.iat[-1])
        # 30 balas (subcuenta) con la vela de BTC
        if self.balas:
            if "BTC" in velas:
                cierres = self.datos.v4("BTC", t, dias=60).c
                self.balas.procesar(t, velas["BTC"], cierres, funding_8h=self.pub.funding("BTC"),
                                    bloqueado=bool(db.get("bloqueado") or db.get("pausado")))
            else:
                db.incidencia("alta", "velas_btc", f"Falta la vela de BTC de {t - CUATRO_H}: 30 balas no decidió este ciclo")
        r = self.motor.ciclo(t, precios=precios, velas_cerradas={k: v[:3] for k, v in velas.items()})
        if r.get("corte") and self.balas:
            self.balas.forzar_salida(t, precios.get("BTC"), "corte")
        self.previsiones(precios)
        db.set("ultimo_ciclo_seg", round(time.time() - t0, 1))
        log.info("ciclo %s listo en %.0f s", t, time.time() - t0)

    def control(self):
        """Control entre ciclos: patrimonio a precio actual, caída, corte y previsiones."""
        cfg, db = self.cfg, self.db
        lib = self.motor.libro()
        bases = sorted({L["simbolo"] for L in lib.values()} | {"BTC", "ETH"})
        precios = self.pub.precios(bases)
        db.set("precios", {**(db.get("precios") or {}), **precios})
        if self.bolsa.modo == "papel":
            self.bolsa.fijar_precios(precios)
        E_main = self.bolsa.patrimonio()
        E_bal = self.balas.patrimonio(precios.get("BTC")) if self.balas else 0.0
        t = _ahora().floor("min")
        er = riesgo.actualizar_patrimonio(db, t, E_main + E_bal, E_main, E_bal, lib, precios, cfg)
        if er["corte"] and not db.get("bloqueado"):
            db.set("bloqueado", True)
            db.incidencia("critica", "corte_caida", f"Caída {er['caida']:.1%} ≥ corte {cfg.corte_caida:.0%}: se cierra todo")
            self.avisar("critica", f"CORTE POR CAÍDA: {er['caida']:.1%}. Cierro todo y bloqueo entradas. Reactivar con /reanudar.")
            self.motor.mercados = self.pub.mercados()
            self.motor.cerrar_todo(t, "corte")
            if self.balas:
                self.balas.forzar_salida(t, precios.get("BTC"), "corte")
        elif er["alerta"]:
            if db.incidencia("alta", "alerta_caida", f"Caída desde el máximo {er['caida']:.1%} ≥ alerta {cfg.alerta_caida:.0%}"):
                self.avisar("alta", f"Alerta: caída desde el máximo {er['caida']:.1%}")
        self.previsiones(precios)

    def previsiones(self, precios):
        fund = {}
        for b in {L["simbolo"] for L in self.motor.libro().values()}:
            fund[b] = self.pub.funding(b)
        bal = None
        if self.balas:
            s = self.balas.st
            bal = dict(dist_liq=s.get("dist_liq"), reserva_usada=bool(s.get("activo") and s.get("resd")))
        antes = {p["tipo"] for p in (self.db.get("previsiones") or []) if p["nivel"] == "alta"}
        out = riesgo.previsiones(self.db, self.cfg, precios, fund, bal)
        for nivel, tipo, msg in out:
            if nivel == "alta" and tipo not in antes:
                self.avisar("alta", f"Posible incidencia: {msg}")

    def resumen_diario(self):
        db = self.db
        t = _ahora()
        ops = db.filas("SELECT COUNT(*) n FROM operaciones WHERE ts>=?", (int((t - pd.Timedelta(days=1)).value // 10**6),))[0]["n"]
        prev = db.get("previsiones") or []
        txt = "Resumen diario\n" + ES.texto_estado(db, self.cfg) + f"\nOperaciones últimas 24 h: {ops}"
        abiertas = db.filas("SELECT nivel, mensaje FROM incidencias WHERE resuelta=0 ORDER BY id DESC LIMIT 5")
        if abiertas:
            txt += "\nIncidencias abiertas:\n" + "\n".join(f"  [{a['nivel']}] {a['mensaje']}" for a in abiertas)
        if prev:
            txt += "\nPrevisiones:\n" + "\n".join(f"  [{p['nivel']}] {p['mensaje']}" for p in prev[:5])
        self.avisar("resumen", txt)

    # ------------------------------------------------------------ comandos (Telegram y web)
    def comando(self, nombre, args):
        db = self.db
        if nombre in ("start", "ayuda", "help"):
            return AYUDA
        if nombre == "estado":
            return ES.texto_estado(db, self.cfg)
        if nombre == "previsiones":
            p = db.get("previsiones") or []
            return "\n".join(f"[{x['nivel']}] {x['mensaje']}" for x in p) or "Sin incidencias previstas."
        if nombre == "incidencias":
            a = db.filas("SELECT ts, nivel, mensaje FROM incidencias WHERE resuelta=0 ORDER BY id DESC LIMIT 15")
            return "\n".join(f"{pd.to_datetime(x['ts'], unit='ms'):%d/%m %H:%M} [{x['nivel']}] {x['mensaje']}" for x in a) or "Sin incidencias abiertas."
        if nombre == "pausar":
            db.set("pausado", True)
            return "Pausado: no se abren lotes nuevos. Los abiertos siguen con sus salidas y stops."
        if nombre == "reanudar":
            db.set("pausado", False)
            if db.get("bloqueado"):
                db.set("bloqueado", False)
                db.set("maximo_patrimonio", (db.get("ultimo_ciclo") or {}).get("E") or db.get("maximo_patrimonio"))
                db.resolver("corte_caida")
                return "Reanudado y desbloqueado. El máximo para medir la caída arranca de nuevo desde el patrimonio actual."
            return "Reanudado."
        if nombre == "cerrar_todo":
            if not args or args[0].lower() != "si":
                return "Esto cierra TODAS las posiciones de la cuenta principal y de 30 balas, y pausa. Confirmá con: /cerrar_todo si"
            t = _ahora().floor("min")
            self.motor.mercados = self.pub.mercados()
            self.motor.cerrar_todo(t, "manual")
            if self.balas:
                self.balas.forzar_salida(t, self.pub.precios(["BTC"]).get("BTC"), "manual")
            db.set("pausado", True)
            return "Cerré todo y quedó pausado. /reanudar para volver a operar."
        if nombre == "nivel":
            c = self.cfg
            return (f"Nivel {c.nivel} (techo intrabarra {c.techo:.0%}), alerta {c.alerta_caida:.0%}, corte {c.corte_caida:.0%}\n"
                    + "\n".join(f"  {k}: {v:.3f}" for k, v in c.pesos.items()) + "\nPara cambiarlo: editar config/nivel.yaml y reiniciar.")
        return "Comando desconocido. /ayuda"

    def atender_comandos(self):
        while not self.tg.comandos.empty():
            nombre, args = self.tg.comandos.get()
            try:
                self.tg.avisar("info", self.comando(nombre, args))
            except Exception as e:
                log.exception("comando %s", nombre)
                self.tg.avisar("alta", f"Error en /{nombre}: {e}"[:500])
        while not self.cola_web.empty():
            nombre, args, respuesta = self.cola_web.get()
            try:
                respuesta.put(self.comando(nombre, args))
            except Exception as e:
                respuesta.put(f"Error: {e}")


def main():
    configurar_log()
    import queue
    s = Sistema()
    s.cola_web = queue.Queue()
    s.db.set("version", VERSION)
    from . import web
    web.arrancar(s, puerto=int(os.environ.get("WEB_PUERTO", "8080")))
    s.tg.escuchar()
    cfg = s.cfg
    E = s.bolsa.patrimonio() + (s.balas.patrimonio() if s.balas else 0)
    m = s.pub.mercados()
    info = {b: m[b]["tam"] for b in ("BTC", "ETH") if b in m}
    s.db.set("mercados_info", info)
    s.avisar("info", f"Cascada {VERSION} iniciada · modo {cfg.modo} · {cfg.nivel} · patrimonio {E:,.2f} USDT"
             + (" · balas desactivada" if "balas5" in cfg.desactivadas else "") + f"\nTamaño de contrato: {info}")
    ultimo = pd.Timestamp(s.db.get("ultimo_t_ciclo") or "2000-01-01")
    ultimo_control = 0.0
    fallos = 0
    while True:
        try:
            s.atender_comandos()
            s.db.set("latido", time.time())
            ahora = _ahora()
            t = ahora.floor("4h")
            if t > ultimo and (ahora - t).total_seconds() >= ESPERA_CIERRE:
                try:
                    s.ciclo(t)
                    ultimo = t; fallos = 0
                    s.db.set("ultimo_t_ciclo", str(t))
                    s.db.resolver("ciclo_fallido")
                    if t.hour == 0:
                        s.resumen_diario()
                except Exception as e:
                    fallos += 1
                    log.error("ciclo %s: %s", t, traceback.format_exc())
                    s.db.incidencia("critica", "ciclo_fallido", f"Ciclo {t} falló ({fallos}): {e}"[:300], 1)
                    if fallos in (1, 5):
                        s.avisar("critica", f"El ciclo {t:%d/%m %H:%M} falló (intento {fallos}): {e}"[:500])
                    if fallos >= 8:
                        ultimo = t; fallos = 0        # se espera el próximo cierre
                    time.sleep(60)
                ultimo_control = time.time()
            elif time.time() - ultimo_control >= CONTROL_MIN * 60:
                try:
                    s.control()
                    s.db.resolver("control_fallido")
                except Exception as e:
                    log.error("control: %s", traceback.format_exc())
                    s.db.incidencia("media", "control_fallido", f"Control de riesgo falló: {e}"[:300])
                ultimo_control = time.time()
        except Exception:
            log.error("bucle: %s", traceback.format_exc())
        time.sleep(5)


if __name__ == "__main__":
    main()
