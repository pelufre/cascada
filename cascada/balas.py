"""30 balas 5x en la subcuenta de futuros inversos (XBTUSDM), con las reglas del backtest del proyecto.

Es el mismo bucle que `cartera_balas.run` (motor sombra de BTC_Hibrida_v2), paso a paso. En cada cierre de vela de 4 h,
en dos tiempos (enmienda E14, segunda auditoría):

  1. `vela()`: lo que pasó DURANTE la vela cerrada con la posición que había: funding de XBTUSDM liquidado en ese tramo
     (cada evento con la posición de ese momento, no una tasa repartida) y liquidación con el mínimo de la vela.
  2. `decidir()`: con el cierre, las series diarias/semanales y las reglas, decide; lo decidido se ejecuta en ese momento,
     que es la apertura de la vela siguiente (backtest: precio de apertura siguiente; papel: precio del momento; real:
     órdenes cuyo resultado se lee de la subcuenta). La reserva (margen extra cuando el precio está a menos de 12 % de la
     liquidación) se decide acá y queda puesta ANTES de la vela siguiente: nunca protege una vela que ya pasó.

Contabilidad (igual al backtest): W = patrimonio de la subcuenta al empezar cada campaña (USD); usd, reserva, etc. son
fracciones de W; el margen se compra en BTC spot (comisión spot y deslizamiento incluidos) y al final de la campaña el BTC
se vende por USDT. Con `contrato_usd` (1 USD por contrato en XBTUSDM) la cantidad se redondea como en KuCoin.

En modo real no se simula nada: después de cada acción y en cada ciclo el estado se lee de la subcuenta (posición,
precio de entrada, BTC y USDT; `sincronizar`). Cada acción real queda registrada antes de mandarse (tabla
`balas_acciones`); si falla o queda sin respuesta, 30 balas se bloquea hasta que la subcuenta quede en un estado
coherente (se completa o se deshace lo que quedó a medias) y se avisa.
"""
import json
import math
import time
import uuid

import numpy as np
import pandas as pd

from .indicadores import rsi_wilder

P = dict(lev=5.0, bullets=30, init=1, ACT=0.5, RES=0.5, PREF=0.25, RESTRIG=0.12, MMR=0.007, FEE=0.0006, SLIP=0.0005,
         FEE_SPOT=0.001, rsi_th=15.0, ma_len=40)
# Tramos de riesgo de KuCoin para XBTUSDM (proyecto: niveles_riesgo_XBTUSDM.csv): (tope de la posición en BTC, margen de
# mantenimiento). Hasta 5 BTC rige el 0,7 % de P["MMR"]: con 3000 USDT en el nivel 30 la posición no pasa de ~0,55 BTC.
TRAMOS_XBTUSDM = [(5, 0.007), (20, 0.01), (40, 0.025), (60, 0.05), (70, 0.1), (80, 0.125), (90, 0.25), (100, 0.5)]
POLVO_USD = 1.0           # saldos menores (en USD) se consideran restos de redondeo


def mmr_xbtusdm(posicion_btc):
    for tope, m in TRAMOS_XBTUSDM:
        if posicion_btc <= tope:
            return m
    return TRAMOS_XBTUSDM[-1][1]


VACIO = dict(activo=False, rec=False, resd=False, used=0, ntn=0.0, inv=0.0, mbtc=0.0, contrib=0.0, entry_t=None)


class Balas:
    def __init__(self, db, capital_papel, real=None, avisar=None, clave="balas", costos=True, contrato_usd=1.0):
        """costos=False: sin comisiones ni deslizamiento (sólo diagnóstico). contrato_usd: tamaño del contrato de XBTUSDM
        para redondear como en KuCoin (1 USD); None = contratos fraccionarios (corrida A)."""
        self.db = db; self.real = real; self.avisar = avisar or (lambda n, t: None); self.clave = clave
        self.fee = P["FEE"] if costos else 0.0
        self.slip = P["SLIP"] if costos else 0.0
        self.fee_spot = P["FEE_SPOT"] if costos else 0.0
        self.contrato = contrato_usd
        st = db.get(clave)
        if st is None:
            st = dict(W=float(capital_papel), **VACIO, dC=[], emaW=None, emaHist=[], regWeekly=False, levm=1.0, momT=False,
                      liqs=0, camps=0, ultima_vela=None, eq=float(capital_papel), dist_liq=None)
            db.set(clave, st)
        for k, v in dict(funding_usd=0.0, comisiones_usd=0.0, resultado_usd=0.0, campanas=[], ultima_decision=None,
                         liq_vela=False, bloqueo=None).items():
            st.setdefault(k, v)
        self.st = st

    # ---------------- utilidades
    def patrimonio(self, precio=None):
        s = self.st
        if not s["activo"]:
            return s["W"]
        C = precio or s.get("ultimo_precio")
        return s["W"] * (1 + (s["mbtc"] + s["inv"] - s["ntn"] / C) * C - s["contrib"])

    def nocional(self):
        """Nocional de la posición en USD (contratos de 1 USD)."""
        return self.st["ntn"] * self.st["W"] if self.st["activo"] else 0.0

    def delta_usd(self, precio=None):
        """Exposición económica en USD al precio de BTC: margen en BTC + valor de entrada de los contratos (para un
        futuro inverso largo, d patrimonio / d ln precio)."""
        s = self.st
        if not s["activo"]:
            return 0.0
        C = precio or s.get("ultimo_precio")
        return (s["mbtc"] + s["inv"]) * s["W"] * C

    def _liq(self):
        s = self.st
        if not (s["activo"] and (s["mbtc"] + s["inv"]) > 0):
            return None
        px = s.get("ultimo_precio")
        mmr = mmr_xbtusdm(s["ntn"] * s["W"] / px) if px else P["MMR"]       # tramo según el tamaño en BTC
        return s["ntn"] * (1 + mmr) / (s["mbtc"] + s["inv"])

    def _q(self, ntn):
        """Nocional (fracción de W) redondeado hacia abajo a contratos enteros de XBTUSDM."""
        if not self.contrato or not self.st["W"]:
            return ntn
        return math.floor(ntn * self.st["W"] / self.contrato + 1e-9) * self.contrato / self.st["W"]

    def _compra(self, usd, px):
        """BTC (por W) que deja comprar `usd` (fracción de W) a mercado en spot: deslizamiento y comisión spot."""
        return usd * (1 - self.fee_spot) / (px * (1 + self.slip))

    def _op(self, t, tipo, precio, nocional_usd, motivo, comision_usd=None):
        self.db.ejec("INSERT INTO operaciones (ts,cuenta,estrategia,lote,simbolo,lado,contratos,precio,nocional,comision,motivo,modo)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (int(pd.Timestamp(t).value // 10**6), "balas", "balas5", f"campaña_{self.st['camps']}", "XBTUSDM",
                      tipo, nocional_usd, precio, nocional_usd,
                      abs(nocional_usd) * self.fee if comision_usd is None else comision_usd, motivo,
                      "real" if self.real else "papel"))

    def _fin_campaña(self, t, resultado_usd, motivo):
        s = self.st
        s["resultado_usd"] += resultado_usd
        s["campanas"].append(dict(n=s["camps"], inicio=s.get("entry_t"), fin=str(t), W0=s["W"], resultado=resultado_usd,
                                  motivo=motivo))
        del s["campanas"][:-500]

    def _dia(self, t, c):
        """Cierre diario c en t (00:00 UTC): serie diaria, EMA semanal (cierre del domingo), régimen, apalancamiento y momento."""
        s = self.st
        dC = s["dC"]; dC.append(c); del dC[:-400]
        if t.dayofweek == 0:   # cierre del domingo
            a = 2.0 / 21
            s["emaW"] = c if s["emaW"] is None else s["emaW"] + a * (c - s["emaW"])
            s["emaHist"].append(s["emaW"]); del s["emaHist"][:-10]
        if len(s["emaHist"]) > 4:
            eL, eS = s["emaHist"][-1], s["emaHist"][-5]
            if t.dayofweek == 0:
                s["regWeekly"] = c > eL and eL > eS
        nd = len(dC); lm = 1.0
        if nd > 60:
            r = np.diff(np.log(dC[-61:])); vol = r.std(ddof=1) * math.sqrt(365)
            if vol > 0: lm = max(0.33, min(1.0, 0.60 / vol))
        if nd >= 200 and c / np.mean(dC[-200:]) > 2.2:
            lm *= 0.5
        s["levm"] = lm
        s["momT"] = (c / dC[-91] - 1 > 0) if nd > 90 else False

    def _calentar(self, t, cierres_4h):
        """Al arrancar sin historia, reconstruye las series diarias y semanales con las velas de 4h disponibles
        (para que el régimen semanal y el momento de 90 días estén listos desde el primer ciclo)."""
        s = self.st
        if s.get("calentado"):
            return
        s["calentado"] = True
        if s["dC"] or cierres_4h is None or not len(cierres_4h):
            return
        for ts, c in cierres_4h.items():
            cierre = pd.Timestamp(ts) + pd.Timedelta(hours=4)
            if cierre.hour == 0 and cierre < t:
                self._dia(cierre, float(c))

    def _guardar(self, t=None):
        self.db.set(self.clave, self.st)
        if t is not None:
            self.db.ejec("INSERT OR REPLACE INTO patrimonio VALUES (?,?,?,?,?,?)",
                         (int(pd.Timestamp(t).value // 10**6), "balas", self.st["eq"], self.nocional(), None, None))

    # ---------------- 1) la vela cerrada
    def _funding(self, tasa, px):
        """Un evento de funding sobre la posición actual (el largo paga si la tasa es positiva), en BTC."""
        s = self.st
        s["mbtc"] -= s["ntn"] * tasa / px
        s["funding_usd"] += s["ntn"] * s["W"] * tasa

    def vela(self, t, vela, eventos_funding=()):
        """Lo que pasó durante la vela que cierra en t, con la posición que había. vela: (o, h, l, c);
        eventos_funding: [(ts, tasa)] de XBTUSDM liquidados en (t − 4 h, t]. Un evento en t (el cierre) cae después de
        cualquier liquidación de la vela; uno dentro de la vela, si es un costo, se aplica antes de mirar la liquidación
        (el orden dentro de la vela no se conoce: se toma el desfavorable). En real no se simula nada."""
        s = self.st; t = pd.Timestamp(t)
        if s.get("ultima_vela") and pd.Timestamp(s["ultima_vela"]) >= t:
            return False
        o, h, l, c = vela
        s["ultima_vela"] = str(t); s["liq_vela"] = False
        if s["activo"] and not self.real:
            eventos = [(pd.Timestamp(ts), float(r)) for ts, r in (eventos_funding or ()) if r == r]
            dentro = [r for ts, r in eventos if ts < t]
            al_cierre = [r for ts, r in eventos if ts >= t]
            for r in dentro:
                if r > 0:
                    self._funding(r, c)
            s["ultimo_precio"] = c
            liq = self._liq()
            if liq is not None and l <= liq:
                perdida = s["contrib"] * s["W"]
                self._op(t, "liquidacion", liq, s["ntn"] * s["W"], "liquidacion", 0.0)
                self._fin_campaña(t, -perdida, "liquidacion")
                s["W"] *= (1 - s["contrib"]); s["liqs"] += 1; s["liq_vela"] = True
                s.update(VACIO)
                self.avisar("critica", f"30 balas: LIQUIDACIÓN a {liq:.0f}")
            else:
                for r in dentro:
                    if r <= 0:
                        self._funding(r, c)
                for r in al_cierre:
                    self._funding(r, c)
        s["ultimo_precio"] = c
        s["eq"] = self.patrimonio(c)
        liq = self._liq()
        s["dist_liq"] = (c / liq - 1) if liq else None
        self._guardar()
        return True

    # ---------------- 2) la decisión al cierre, ejecutada en ese momento
    def decidir(self, t, cierres_4h, bloqueado=False, px=None):
        """Decide con el cierre de la vela que cierra en t y ejecuta a `px` (backtest: apertura de la vela siguiente;
        papel: precio del momento; real: lo que llene el exchange). cierres_4h: cierres hasta esa vela (RSI(2), SMA40)."""
        s = self.st; t = pd.Timestamp(t)
        if s.get("ultima_decision") and pd.Timestamp(s["ultima_decision"]) >= t:
            return
        self._calentar(t, cierres_4h)
        c = float(cierres_4h.iat[-1]) if cierres_4h is not None and len(cierres_4h) else s.get("ultimo_precio")
        px = float(px or c)
        if self.real and not self._preparar_real(t, px):
            return                                      # sin lectura de la subcuenta o bloqueada: no decide
        s["ultima_decision"] = str(t)
        if t.hour == 0:
            self._dia(t, c)
        reg_today = s["regWeekly"]
        cs = cierres_4h
        rsi = rsi_wilder(cs).iat[-1]
        ma = cs.rolling(P["ma_len"]).mean()
        above = c > ma.iat[-1]
        prev_above = len(cs) > 1 and cs.iat[-2] > ma.iat[-2]
        acciones = []
        if s["activo"]:
            roe = (s["mbtc"] + s["inv"] - s["ntn"] / c) / s["mbtc"] - 1 if s["mbtc"] > 0 else 0.0
            salir = (s["rec"] and prev_above and not above) or (t.hour == 0 and not reg_today)
            s["rec"] = s["rec"] or above
            if salir:
                acciones.append(("salir",))
                if reg_today and rsi <= P["rsi_th"] and s["momT"] and not bloqueado:
                    acciones.append(("entrar", s["levm"], above))
            elif t.hour == 0 and s["used"] < P["bullets"] and not bloqueado:
                add = 1 if roe >= 0 else 2 if roe >= -0.05 else 4 if roe >= -0.10 else 6 if roe >= -0.15 else 8
                add = min(add, P["bullets"] - s["used"])
                u = P["ACT"] * add / P["bullets"]
                ratio = 1.0 if roe >= -0.10 else 0.7 if roe >= -0.20 else 0.4 if roe >= -0.30 else 0.0
                acciones.append(("recargar", u, u * ratio, s["levm"], add))
        elif not s.get("liq_vela") and reg_today and rsi <= P["rsi_th"] and s["momT"] and not bloqueado:
            acciones.append(("entrar", s["levm"], above))
        for a in acciones:
            if not self._ejecutar(t, a, px):
                break
        # reserva para la vela siguiente: se decide ahora y queda puesta antes de que empiece
        if s["activo"] and not s["resd"] and not s.get("bloqueo"):
            s["ultimo_precio"] = px
            liq = self._liq()
            if liq and px / liq - 1 <= P["RESTRIG"]:
                self._ejecutar(t, ("reserva",), px)
        s["ultimo_precio"] = px
        s["eq"] = self.patrimonio(px)
        liq = self._liq()
        s["dist_liq"] = (px / liq - 1) if liq else None
        self._guardar(t)

    def procesar(self, t, vela, cierres_4h, eventos_funding=(), bloqueado=False, px=None):
        """La vela cerrada y la decisión, en ese orden (servicio y pruebas)."""
        self.vela(t, vela, eventos_funding)
        self.decidir(t, cierres_4h, bloqueado=bloqueado, px=px)

    # ---------------- ejecución (simulada o real)
    def _ejecutar(self, t, a, px):
        if self.real:
            return self._ejecutar_real(t, a, px)
        s = self.st; W = s["W"]
        if a[0] == "salir" and s["activo"]:
            pe = px * (1 - self.slip)
            btc = s["mbtc"] + s["inv"] - s["ntn"] / pe - s["ntn"] / pe * self.fee      # BTC tras cerrar los contratos
            pnl = btc * pe * (1 - self.fee_spot) - s["contrib"]                          # y vender el BTC por USDT
            com = (s["ntn"] * self.fee + btc * pe * self.fee_spot) * W
            s["comisiones_usd"] += com
            self._op(t, "sell", pe, s["ntn"] * W, "salida", com)
            self._fin_campaña(t, W * pnl, "salida")
            s["W"] = W * (1 + pnl)
            self.avisar("op", f"30 balas: fin de campaña, resultado {pnl:+.1%} de la subcuenta")
            s.update(VACIO)
        elif a[0] == "entrar":
            usd = P["ACT"] * P["init"] / P["bullets"]
            margen = usd + P["RES"] * P["PREF"]
            q = self._q(usd * P["lev"] * a[1])
            s["ntn"] = q
            s["inv"] = q / (px * (1 + self.slip))
            s["mbtc"] = self._compra(margen, px) - q * self.fee / px
            s["contrib"] = margen
            s.update(resd=False, activo=True, entry_t=str(t), used=P["init"], rec=a[2])
            s["camps"] += 1
            com = (q * self.fee + margen * self.fee_spot) * W
            s["comisiones_usd"] += com
            self._op(t, "buy", px, q * W, "entrada", com)
            self.avisar("op", f"30 balas: nueva campaña a {px:.0f} (apalancamiento {P['lev'] * a[1]:.1f}x)")
        elif a[0] == "recargar" and s["activo"]:
            _, u, upos, lvm, add = a
            q = self._q(upos * P["lev"] * lvm) if upos > 0 else 0.0
            s["contrib"] += u
            s["ntn"] += q; s["inv"] += q / (px * (1 + self.slip))
            s["mbtc"] += self._compra(u, px) - q * self.fee / px
            s["used"] += add
            com = (q * self.fee + u * self.fee_spot) * W
            s["comisiones_usd"] += com
            self._op(t, "buy", px, q * W, f"recarga {add} balas", com)
        elif a[0] == "reserva" and s["activo"]:
            cash = P["RES"] * (1 - P["PREF"])
            s["mbtc"] += self._compra(cash, px); s["contrib"] += cash; s["resd"] = True
            com = cash * self.fee_spot * W
            s["comisiones_usd"] += com
            self._op(t, "margen", px, 0.0, "reserva", com)
            self.avisar("alta", "30 balas: entra la reserva para la vela siguiente (precio a menos de 12 % de la liquidación)")
        return True

    def forzar_salida(self, t, px, motivo="corte"):
        """Cierra la campaña ya (corte por caída o pedido manual)."""
        s = self.st
        if not s["activo"]:
            return
        if self.real:
            self._ejecutar_real(t, ("salir",), px, motivo=motivo)
            s["eq"] = self.patrimonio(px); s["dist_liq"] = None
            self._guardar()
            return
        self._ejecutar(t, ("salir",), px)
        s["eq"] = s["W"]; s["dist_liq"] = None
        self._guardar()
        self.avisar("alta", f"30 balas: campaña cerrada por {motivo}")

    # ---------------- modo real: la subcuenta manda
    def sincronizar(self, px, esperado=None):
        """Lee la subcuenta y deja el estado igual a lo que hay: contratos, precio de entrada, BTC (futuros y spot) y
        USDT. esperado: "entrada" o "salida" si la acción que acaba de mandarse abre o cierra la campaña; si no, una
        posición nueva es ajena y una que desaparece es una liquidación o un cierre manual. Devuelve lo leído o None."""
        s = self.st
        try:
            r = self.real.estado()
        except Exception as e:
            if self.db.incidencia("alta", "balas_lectura", f"30 balas: no pude leer la subcuenta: {e}"[:300], 6):
                self.avisar("alta", f"30 balas: no pude leer la subcuenta ({e}); no decide este ciclo"[:400])
            return None
        self.db.resolver("balas_lectura")
        N = float(r.get("contratos") or 0.0)
        btc = float(r.get("margen_btc") or 0.0) + float(r.get("btc_spot") or 0.0)
        usdt = float(r.get("usdt") or 0.0)
        if N >= 1:
            E = float(r.get("entrada") or px)
            if not s["activo"]:
                if esperado != "entrada":           # posición que el estado no conocía (reinicio sin estado)
                    s["W"] = usdt + btc * px + N * (px / E - 1)
                    s.update(used=P["init"], rec=False, resd=False, entry_t=None)
                    self.db.incidencia("alta", "balas_posicion_ajena", f"30 balas: la subcuenta tenía {N:g} contratos que "
                                       "el estado no registraba: se toman como campaña abierta")
                s["activo"] = True
            W = s["W"]
            s["ntn"] = N / W; s["inv"] = N / E / W; s["mbtc"] = btc / W; s["contrib"] = 1 - usdt / W
        else:
            if s["activo"]:
                eq = usdt + btc * px
                if esperado == "salida":
                    self._fin_campaña(pd.Timestamp.now("UTC").tz_localize(None), eq - s["W"], "salida")
                else:                               # la campaña terminó sin que la cerráramos
                    self._fin_campaña(pd.Timestamp.now("UTC").tz_localize(None), eq - s["W"], "externa")
                    s["liqs"] += 1
                    self.db.incidencia("critica", "balas_externa", "30 balas: la posición desapareció de la subcuenta "
                                       "(liquidación o cierre manual)")
                    self.avisar("critica", "30 balas: la posición ya no está en la subcuenta (¿liquidación?). Revisar.")
            s.update(VACIO)
            s["W"] = usdt + btc * px
        s["ultimo_precio"] = px
        s["eq"] = self.patrimonio(px)
        return r

    def _preparar_real(self, t, px):
        """Antes de decidir en real: leer la subcuenta y, si quedó algo a medias, completarlo o deshacerlo."""
        r = self.sincronizar(px)
        if r is None:
            self._guardar()
            return False
        s = self.st
        resto_usd = float(r.get("btc_spot") or 0.0) * px
        suelto_usd = (float(r.get("margen_btc") or 0.0) + float(r.get("btc_spot") or 0.0)) * px
        pendiente = None
        if s["activo"] and resto_usd > POLVO_USD:
            pendiente = ("completar_margen", lambda: self.real.a_futuros_todo())       # BTC comprado que no llegó a futuros
        elif not s["activo"] and suelto_usd > POLVO_USD:
            pendiente = ("vender_resto", lambda: self.real.cerrar(px))                  # BTC de una campaña que no se vendió
        if pendiente:
            if not self._accion_real(t, pendiente[0], pendiente[1], dict(px=px)):
                self._guardar()
                return False
            if self.sincronizar(px) is None:
                self._guardar()
                return False
        if s.get("bloqueo"):
            self.db.ejec("UPDATE balas_acciones SET estado='resuelta' WHERE id=? AND estado='incierta'", (s["bloqueo"],))
            s["bloqueo"] = None
            self.db.resolver("balas_accion")
            self.avisar("alta", "30 balas: la subcuenta quedó coherente; se levanta el bloqueo")
        return True

    def _accion_real(self, t, tipo, fn, pedido):
        """Anota la acción ANTES de mandarla, la manda y anota el resultado. Si falla o queda sin respuesta, 30 balas queda
        bloqueada (no decide hasta que la subcuenta esté coherente) y se avisa."""
        aid = uuid.uuid4().hex
        self.db.ejec("INSERT INTO balas_acciones (id, ts, vela, tipo, estado, pedido) VALUES (?,?,?,?,?,?)",
                     (aid, int(time.time() * 1000), str(t), tipo, "enviando", json.dumps(pedido, default=str)))
        try:
            res = fn()
        except Exception as e:
            self.db.ejec("UPDATE balas_acciones SET estado='incierta', error=? WHERE id=?", (str(e)[:300], aid))
            self.st["bloqueo"] = aid
            self.db.incidencia("critica", "balas_accion", f"30 balas: {tipo} falló o quedó sin respuesta: {e}"[:300])
            self.avisar("critica", f"30 balas: {tipo} falló o quedó sin confirmar ({e}). Bloqueada hasta que la subcuenta "
                                   "quede coherente."[:400])
            return False
        self.db.ejec("UPDATE balas_acciones SET estado='hecha', resultado=? WHERE id=?",
                     (json.dumps(res, default=str)[:2000] if res is not None else None, aid))
        return True

    def _ejecutar_real(self, t, a, px, motivo="salida"):
        """Traduce la acción a órdenes y después lee la subcuenta: cantidades, precios, comisiones y BTC son los reales.
        Lo que la regla recuerda y la subcuenta no dice (balas usadas, reserva puesta) se anota según lo que se ve."""
        s = self.st; W = s["W"]
        antes = dict(activo=s["activo"], ntn=s["ntn"] * W, contrib=s["contrib"] * W)
        if a[0] == "salir":
            ok = self._accion_real(t, "cerrar", lambda: self.real.cerrar(px), dict(px=px, motivo=motivo))
            esperado = "salida"
        elif a[0] == "entrar":
            usd = P["ACT"] * P["init"] / P["bullets"]
            margen = usd + P["RES"] * P["PREF"]
            n = self._q(usd * P["lev"] * a[1]) * W
            ok = self._accion_real(t, "abrir", lambda: self.real.abrir(margen * W, n, px), dict(margen=margen * W, contratos=n, px=px))
            esperado = "entrada"
        elif a[0] == "recargar":
            _, u, upos, lvm, add = a
            n = self._q(upos * P["lev"] * lvm) * W if upos > 0 else 0.0
            ok = self._accion_real(t, f"recarga {add}", lambda: self.real.recargar(u * W, n, px), dict(margen=u * W, contratos=n, px=px))
            esperado = None
        elif a[0] == "reserva":
            cash = P["RES"] * (1 - P["PREF"])
            ok = self._accion_real(t, "reserva", lambda: self.real.aportar_margen(cash * W, px), dict(margen=cash * W, px=px))
            esperado = None
        else:
            return True
        if self.sincronizar(px, esperado) is None:
            s["bloqueo"] = s.get("bloqueo") or "sin_lectura"
            return False
        if a[0] == "entrar" and s["activo"] and not antes["activo"]:
            s["camps"] += 1
            s.update(resd=False, entry_t=str(t), used=P["init"], rec=a[2])
            self._op(t, "buy", px, s["ntn"] * s["W"], "entrada", 0.0)
            self.avisar("op", f"30 balas: nueva campaña, {s['ntn'] * s['W']:.0f} contratos")
        elif a[0] == "recargar" and s["activo"] and (s["ntn"] * W > antes["ntn"] + 0.5 or s["contrib"] * W > antes["contrib"] + 0.01):
            s["used"] += a[4]
            self._op(t, "buy", px, s["ntn"] * W - antes["ntn"], f"recarga {a[4]} balas", 0.0)
        elif a[0] == "reserva" and s["activo"] and s["contrib"] * W > antes["contrib"] + 0.01:
            s["resd"] = True
        elif a[0] == "salir" and not s["activo"]:
            self._op(t, "sell", px, antes["ntn"], motivo, 0.0)
            self.avisar("op" if motivo == "salida" else "alta", f"30 balas: campaña cerrada ({motivo})")
        return ok


from .balas_real import EjecutorRealBalas  # noqa: E402,F401  (ejecución real, ver balas_real.py)
