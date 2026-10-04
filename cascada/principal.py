"""Servicio principal: ciclo de 4 h, controles cada 15 min, universo semanal, Telegram y web.

    python -m cascada.principal
"""
import logging
import logging.handlers
import os
import threading
import time
import traceback

import pandas as pd

from . import config as C
from . import estadisticas as ES
from . import riesgo
from .balas import Balas, EjecutorRealBalas
from .bolsa import INVERSO, KucoinReal, Papel, Publico
from .datos import Datos, VolumenPerp
from .db import Base
from .motor import Motor
from .subcuenta import TransferenciaIncierta, TransferidorSubcuenta, planificar
from .telegram import AYUDA, Telegram
from . import x as XM

VERSION = "1.3.1"
log = logging.getLogger("cascada")
CUATRO_H = pd.Timedelta(hours=4)
ESPERA_CIERRE = 20          # segundos después del cierre de vela antes de decidir
REINTENTOS_VELAS = (30, 60)   # segundos de espera si falta la vela recién cerrada
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

    def __init__(self, cfg=None, avisar=None, publico=None, ruta_db=None, publico_control=None):
        self.cfg = cfg = cfg or C.cargar()
        self.cerrojo = threading.RLock()      # ciclo, control y comandos que operan no se pisan
        self.db = Base(ruta_db or C.ruta_base(cfg))
        self.tg = Telegram(cfg.telegram_token, cfg.telegram_chat)
        self.avisar = avisar or self.tg.avisar
        self.pub = publico or Publico()
        # el control de riesgo corre en su propio hilo con su propia conexión pública
        self.pub_control = publico_control or (publico if publico is not None else Publico())
        self.x = XM.X(cfg.x, avisar=self.tg.avisar, max_dia=cfg.x_max_dia) if cfg.x_publicar else None
        self.datos = Datos(self.db, self.pub)
        self.volumen = VolumenPerp(self.pub)
        w_bal = cfg.pesos.get("balas5", 0.0)
        self.transferidor = None
        if cfg.modo == "real":
            if not cfg.kucoin.get("apiKey"):
                raise SystemExit("Modo real sin claves de KuCoin en .env")
            self.bolsa = KucoinReal(cfg.kucoin, self.pub, cfg.apalancamiento_exchange, cfg.deslizamiento_max)
            real_balas = None
            if cfg.balas_real and cfg.kucoin_balas.get("apiKey"):
                real_balas = EjecutorRealBalas(cfg.kucoin_balas)
            elif w_bal > 0:
                cfg.pesos["balas5"] = 0.0          # sin subcuenta real, balas no participa
                if "balas5" not in cfg.desactivadas:
                    cfg.desactivadas.append("balas5")
            self.balas = Balas(self.db, cfg.capital_balas_real, real=real_balas, avisar=self.avisar) if real_balas else None
            if real_balas and cfg.balas_transferir:
                self.transferidor = TransferidorSubcuenta(cfg.kucoin_balas_uid, real_balas, cred_principal=cfg.kucoin, db=self.db)
        else:
            cap = cfg.capital_papel
            self.bolsa = Papel(self.db, self.pub.mercados(), capital=cap * (1 - w_bal), comision=cfg.comision,
                               desliz=dict(cfg.deslizamiento_papel))
            self.balas = Balas(self.db, cap * w_bal, avisar=self.avisar) if w_bal > 0 else None
        if self.balas and self.db.get("balas_inicial") is None:
            self.db.set("balas_inicial", self.balas.st["W"])
        self.motor = Motor(cfg, self.db, self.datos, self.bolsa, self.pub, avisar=self.avisar,
                           balas_patrimonio=lambda: self.balas.patrimonio() if self.balas else 0.0,
                           balas_plano=lambda: not (self.balas and self.balas.st.get("activo")),
                           balas_nocional=lambda: (self.balas.st["ntn"] * self.balas.st["W"])
                           if self.balas and self.balas.st.get("activo") else 0.0)

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
        with self.cerrojo:
            return self._ciclo(t)

    def _ciclo(self, t):
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
        try:
            _, sin_vol = self.datos.registrar_volumen(self.volumen, set(self.datos.universo(t)) | {"BTC", "ETH"}, t)
            db.resolver("volumen")
            if sin_vol:
                db.incidencia("baja", "volumen_faltante", "Sin volumen de perpetuo (Binance ni KuCoin), quedan fuera de "
                              "Momentum alts: " + ", ".join(sin_vol)[:250], 24)
        except Exception as e:
            db.incidencia("media", "volumen", f"No pude registrar el volumen de los perpetuos (filtro de liquidez de c40): {e}"[:300])
        bases = self.bases(t)
        hasta = int(t.value // 10**6)
        errores = self.datos.actualizar(bases, hasta_ms=hasta)
        # si a alguna le falta la vela recién cerrada (el exchange a veces tarda), reintenta un par de veces
        for espera in REINTENTOS_VELAS:
            faltan = [b for b in bases if (self.db.ultimo_ts(b, "4h") or 0) < hasta - 4 * 3600_000]
            if not faltan:
                break
            time.sleep(espera)
            errores += self.datos.actualizar(faltan, hasta_ms=hasta)
        faltan = [b for b in bases if (self.db.ultimo_ts(b, "4h") or 0) < hasta - 4 * 3600_000]
        if faltan:
            db.incidencia("media", "velas_atrasadas", f"Sin la vela de {t - CUATRO_H:%d/%m %H:%M}: " + ", ".join(faltan)[:250])
        else:
            db.resolver("velas_atrasadas")
            db.resolver("velas_btc")
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
        if self.bolsa.modo == "papel":
            # primero lo que pasó durante la vela: stops y después el funding, con la posición que quedó (E14)
            self.bolsa.fijar_precios(precios)
            antes = {b: p["c"] for b, p in self.bolsa.st["pos"].items()}
            self.bolsa.revisar_stops({k: v[:3] for k, v in velas.items()})
            self.funding_papel(t, antes, {b: v[3] for b, v in velas.items()})
        # 30 balas (subcuenta) con la vela de BTC: la vela cerrada, el capital entre campañas y la decisión
        if self.balas:
            if "BTC" in velas:
                self.balas.vela(t, velas["BTC"], self.eventos_funding_balas(t))
                self.capital_balas(velas["BTC"][3])
                # al arrancar, la historia larga reconstruye sus series diarias y semanales
                cierres = self.datos.v4("BTC", t, dias=60 if self.balas.st.get("calentado") else 420).c
                self.balas.decidir(t, cierres, px=precios.get("BTC"), bloqueado=bool(
                    db.get("bloqueado") or db.get("pausado") or db.get("balas_bloqueo_transferencia")))
            else:
                db.incidencia("alta", "velas_btc", f"Falta la vela de BTC de {t - CUATRO_H}: 30 balas no decidió este ciclo")
        self.motor.ciclo(t, precios=precios, velas_cerradas={})
        if db.get("liquidando"):
            self.liquidar_todo(t, "corte", precios.get("BTC"))
        self.previsiones(precios)
        self.publicar_x(t)
        db.set("ultimo_ciclo_seg", round(time.time() - t0, 1))
        log.info("ciclo %s listo en %.0f s", t, time.time() - t0)

    def capital_balas(self, px_btc):
        """Protocolo E4, como en la validación: mientras 30 balas no tiene campaña abierta, su capital se iguala a
        peso × patrimonio total (transferencia interna en papel). Durante la campaña no se toca. En real la subcuenta
        tiene su propio capital y, con `balas_transferir: true`, se iguala con transferencias reales (ver subcuenta.py)."""
        w = self.cfg.pesos.get("balas5", 0.0)
        s = self.balas.st
        if s.get("activo") or w <= 0:
            return
        if self.bolsa.modo != "papel":
            if self.transferidor:
                self.reequilibrar_subcuenta(px_btc, w)
            return
        E = self.bolsa.patrimonio() + self.balas.patrimonio(px_btc)
        objetivo = w * E
        if E <= 0 or abs(objetivo - s["W"]) < 0.01:
            return
        self.bolsa.st["caja"] -= objetivo - s["W"]
        self.bolsa._guardar()
        s["W"] = objetivo; s["eq"] = objetivo
        self.db.set(self.balas.clave, s)

    def reequilibrar_subcuenta(self, px_btc, w):
        """Real: transfiere entre la principal y la subcuenta la diferencia con peso × patrimonio total, y deja el
        capital del modelo de 30 balas igual a lo que de verdad hay en la subcuenta. Si una transferencia anterior quedó
        incierta, primero se resuelve con los saldos; mientras no se resuelva no se transfiere y 30 balas no abre
        campaña (`balas_bloqueo_transferencia`)."""
        db, s = self.db, self.balas.st
        if db.get("liquidando"):
            return
        try:
            pendientes = self.transferidor.resolver()
            if pendientes:
                db.set("balas_bloqueo_transferencia", True)
                msg = ("Transferencia de 30 balas sin resolver (" + ", ".join(f"{p['accion']} {p['monto']:.2f} USDT: "
                       f"{p['estado']}" for p in pendientes) + "). No se transfiere ni se abre campaña: revisar saldos")
                if db.incidencia("critica", "transferencia_incierta", msg[:300], 24):
                    self.avisar("critica", msg[:400])
                return
            db.set("balas_bloqueo_transferencia", False)
            db.resolver("transferencia_incierta")
            usdt, btc_usd = self.transferidor.saldo_sub(px_btc)
            E_main = self.bolsa.patrimonio()
            plan = planificar(E_main, self.bolsa.usdt_libre(), usdt, btc_usd, w)
            if plan["accion"] == "enviar":
                self.transferidor.enviar(plan["monto"])
            elif plan["accion"] == "traer":
                self.transferidor.traer(plan["monto"])
            antes = usdt + btc_usd
            if plan["accion"] != "nada":
                time.sleep(2)
                usdt, btc_usd = self.transferidor.saldo_sub(px_btc)
                reg = db.get("transferencias_balas") or []
                reg.append(dict(ts=time.time(), accion=plan["accion"], monto=plan["monto"], objetivo=round(plan["objetivo"], 2),
                                E_main=round(E_main, 2), sub_antes=round(antes, 2), sub_despues=round(usdt + btc_usd, 2)))
                db.set("transferencias_balas", reg[-200:])
                self.avisar("info", f"30 balas: {'envié' if plan['accion'] == 'enviar' else 'traje'} {plan['monto']:.2f} USDT "
                                    f"({'principal → subcuenta' if plan['accion'] == 'enviar' else 'subcuenta → principal'}); "
                                    f"subcuenta {antes:.2f} → {usdt + btc_usd:.2f}, objetivo {plan['objetivo']:.2f}")
            if plan["motivo"] and plan["motivo"] != "diferencia chica":
                db.incidencia("media", "balas_capital", f"30 balas: {plan['motivo']}"[:300], 24)
            s["W"] = s["eq"] = usdt + btc_usd          # la próxima campaña arranca con lo que de verdad hay
            db.set(self.balas.clave, s)
            db.resolver("transferencia_balas")
        except TransferenciaIncierta as e:
            db.set("balas_bloqueo_transferencia", True)
            log.exception("transferencia incierta")
            db.incidencia("critica", "transferencia_incierta", f"Transferencia de 30 balas incierta: {e}"[:300])
            self.avisar("critica", f"Transferencia de 30 balas sin confirmar: {e}. Bloqueo 30 balas hasta resolverla."[:400])
        except Exception as e:
            log.exception("reequilibrar subcuenta")
            if db.incidencia("alta", "transferencia_balas", f"No pude igualar el capital de 30 balas: {e}"[:300]):
                self.avisar("alta", f"No pude igualar el capital de 30 balas con la subcuenta: {e}"[:400])

    def funding_papel(self, t, antes=None, cierres=None):
        """Cobra o paga en papel el funding liquidado de cada posición (cada 8 h en KuCoin, o lo que diga el contrato),
        evento por evento con la posición de ese momento: los stops de la vela ya se procesaron y `antes` es la posición
        al empezar la vela. Marca por símbolo: el último evento visto (un evento publicado con demora no se pierde)."""
        marcas = self.db.get("funding_papel_marcas") or {}
        hasta = int(pd.Timestamp(t).value // 10**6)
        antes = antes or {}
        en_juego = set(antes) | set(self.bolsa.st["pos"])
        marcas = {b: v for b, v in marcas.items() if b in en_juego}       # sin posición se olvida la marca
        pagado = 0.0
        for b in sorted(en_juego):
            desde = marcas.get(b, hasta - 4 * 3600_000)
            tasas = self.pub.funding_liquidado(b, desde)
            if tasas is None:
                self.db.incidencia("media", "funding_papel", f"No pude leer el funding de {b}: el papel lo aplica en el próximo ciclo")
                marcas[b] = desde
                continue
            ev = [(pd.to_datetime(ts, unit="ms"), r) for ts, r in tasas if ts <= hasta]
            px = (cierres or {}).get(b) or self.bolsa.st["ultimo"].get(b)
            pagado += sum(self.bolsa.funding_vela(t, {b: ev}, antes, {b: px}).values())
            marcas[b] = max([desde] + [ts for ts, r in tasas if ts <= hasta])
        self.db.set("funding_papel_marcas", marcas)
        self.db.set("funding_papel_total", (self.db.get("funding_papel_total") or 0.0) + pagado)

    def eventos_funding_balas(self, t):
        """Papel: funding de XBTUSDM (el contrato inverso que opera 30 balas, no el perpetuo USDT) liquidado desde el
        último evento visto. En real lo cobra el exchange y el estado se lee de la subcuenta."""
        if not self.balas or self.balas.real:
            return ()
        hasta = int(pd.Timestamp(t).value // 10**6)
        desde = self.db.get("funding_balas_ms") or hasta - 4 * 3600_000
        ev = self.pub.funding_liquidado("BTC", desde, simbolo=INVERSO)
        if ev is None:
            self.db.incidencia("media", "funding_balas", "No pude leer el funding de XBTUSDM: 30 balas lo aplica en el próximo ciclo")
            return ()
        ev = [(ts, r) for ts, r in ev if ts <= hasta]
        if ev:
            self.db.set("funding_balas_ms", max(ts for ts, _ in ev))
        elif self.db.get("funding_balas_ms") is None:
            self.db.set("funding_balas_ms", desde)
        return [(pd.to_datetime(ts, unit="ms"), r) for ts, r in ev]

    def liquidar_todo(self, t, motivo, px_btc=None):
        """Cierra la cuenta principal y 30 balas. Mientras algo quede abierto el estado sigue en «liquidando» y se
        reintenta en el próximo ciclo o control. Devuelve True si todo quedó plano."""
        db = self.db
        db.set("liquidando", True)
        plano = self.motor.liquidar(t, motivo)
        if self.balas and self.balas.st.get("activo"):
            try:
                self.balas.forzar_salida(t, px_btc or self.pub_control.precios(["BTC"]).get("BTC"), motivo)
            except Exception as e:
                log.exception("forzar salida de balas")
                db.incidencia("critica", "liquidar_balas", f"No pude cerrar 30 balas: {e}"[:300])
                self.avisar("critica", f"No pude cerrar 30 balas: {e}"[:400])
        if plano and not (self.balas and self.balas.st.get("activo")):
            self.motor.liquidar(t, motivo)          # confirma y sale del estado «liquidando»
            return True
        return False

    def control(self):
        """Control entre ciclos (hilo propio): patrimonio a precio actual, caída, corte y previsiones."""
        if not self.cerrojo.acquire(timeout=120):
            self.db.incidencia("alta", "control_esperando", "El control de riesgo no pudo correr: el ciclo lleva más de 2 min ocupado")
            return
        try:
            self._control()
        finally:
            self.cerrojo.release()

    def _control(self):
        cfg, db = self.cfg, self.db
        lib = self.motor.libro()
        bases = sorted({L["simbolo"] for L in lib.values()} | {"BTC", "ETH"})
        precios = self.pub_control.precios(bases)
        db.set("precios", {**(db.get("precios") or {}), **precios})
        if self.bolsa.modo == "papel":
            self.bolsa.fijar_precios(precios)
        E_main = self.bolsa.patrimonio()
        if self.balas and self.balas.real and precios.get("BTC"):
            self.balas.sincronizar(precios["BTC"])          # en real, el patrimonio de balas es el de la subcuenta
        E_bal = self.balas.patrimonio(precios.get("BTC")) if self.balas else 0.0
        t = _ahora().floor("min")
        er = riesgo.actualizar_patrimonio(db, t, E_main + E_bal, E_main, E_bal, lib, precios, cfg)
        if er["corte"] and not db.get("bloqueado"):
            self.motor.iniciar_corte(er["caida"])
        if db.get("liquidando"):
            self.motor.precios.update(precios)
            if self.liquidar_todo(t, "corte", precios.get("BTC")):
                self.publicar_x()
        elif er["alerta"]:
            if db.incidencia("alta", "alerta_caida", f"Caída desde el máximo {er['caida']:.1%} ≥ alerta {cfg.alerta_caida:.0%}"):
                self.avisar("alta", f"Alerta: caída desde el máximo {er['caida']:.1%}")
        self.previsiones(precios)

    def publicar_x(self, t=None, diario=False):
        """Un post con lo ocurrido desde el último publicado (aperturas, cierres, stops, 30 balas)."""
        if not self.x or not self.x.activo:
            return
        try:
            ahora = int(time.time() * 1000) if t is None else int(pd.Timestamp(t).value // 10**6)
            desde = self.db.get("x_ultimo_ms")
            if desde is None:          # primera vez: no publica la historia previa
                self.db.set("x_ultimo_ms", ahora)
                return
            k = ES.kpis(self.db, self.cfg)
            if self.cfg.x_operaciones and ahora > desde:
                txt = XM.texto_operaciones(self.db, self.cfg, desde, ahora, k)
                if txt:
                    self.x.publicar(txt)
            self.db.set("x_ultimo_ms", max(ahora, desde))
            if diario and self.cfg.x_resumen_diario:
                txt = XM.texto_resumen_diario(self.db, self.cfg, k, t)
                if txt:
                    self.x.publicar(txt)
        except Exception:
            log.exception("publicar en X")

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
            if db.get("liquidando"):
                return "Todavía hay posiciones cerrándose (estado «liquidando»). Esperá a que quede todo plano."
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
            db.set("pausado", True)
            with self.cerrojo:
                self.motor.mercados = self.pub.mercados()
                ok = self.liquidar_todo(t, "manual")
            self.publicar_x()
            if ok:
                return "Cerré todo y quedó pausado. /reanudar para volver a operar."
            return "Quedó algo sin cerrar: sigo intentando en cada control (cada 15 min) y aviso cuando esté plano. Pausado."
        if nombre == "nivel":
            c = self.cfg
            return (f"Nivel {c.nivel} (p95 de caída 2020–23 {c.p95_is or 0:.0%}), alerta {c.alerta_caida:.0%}, corte {c.corte_caida:.0%}\n"
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


def hilo_control(s):
    """Control de riesgo en su propio hilo: cada 15 min (cada minuto mientras haya un cierre total pendiente),
    aunque el bucle principal esté ocupado o esperando."""
    ultimo = 0.0
    while True:
        try:
            espera = 60 if s.db.get("liquidando") else CONTROL_MIN * 60
            if time.time() - ultimo >= espera:
                ultimo = time.time()
                s.control()
                s.db.set("latido_control", time.time())
                s.db.resolver("control_fallido")
        except Exception as e:
            log.error("control: %s", traceback.format_exc())
            try:
                if s.db.incidencia("alta", "control_fallido", f"Control de riesgo falló: {e}"[:300]):
                    s.avisar("alta", f"El control de riesgo falló: {e}"[:400])
            except Exception:
                pass
        time.sleep(20)


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
    s.db.set("inicio", time.time())
    threading.Thread(target=hilo_control, args=(s,), daemon=True, name="control").start()
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
                        s.publicar_x(t, diario=True)
                except Exception as e:
                    fallos += 1
                    log.error("ciclo %s: %s", t, traceback.format_exc())
                    s.db.incidencia("critica", "ciclo_fallido", f"Ciclo {t} falló ({fallos}): {e}"[:300], 1)
                    if fallos in (1, 5):
                        s.avisar("critica", f"El ciclo {t:%d/%m %H:%M} falló (intento {fallos}): {e}"[:500])
                    if fallos >= 8:
                        ultimo = t; fallos = 0        # se espera el próximo cierre
                    time.sleep(60)
        except Exception:
            log.error("bucle: %s", traceback.format_exc())
        time.sleep(5)


if __name__ == "__main__":
    main()
