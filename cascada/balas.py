"""30 balas 5x en la subcuenta de futuros inversos (XBTUSDM), con las reglas del backtest del proyecto.

Es el mismo bucle que `cartera_balas.run` (motor sombra de BTC_Hibrida_v2), pero paso a paso: en cada cierre de vela de 4h
se procesan reserva, funding y liquidación de la vela cerrada, se valoriza al cierre, se actualizan las series diarias y
semanales y se decide; lo decidido se ejecuta en la apertura siguiente, que es el mismo momento.

Contabilidad (igual al backtest): W = patrimonio de la subcuenta al empezar cada campaña (USD); las cantidades usd, reserva,
etc. son fracciones de W; el margen se compra en BTC al precio de cada aporte (futuro inverso con margen en BTC).
En modo papel todo se simula. En modo real cada acción se traduce a órdenes de KuCoin (ver `EjecutorRealBalas`),
que está deshabilitado por defecto hasta probarlo con montos mínimos.
"""
import math

import numpy as np
import pandas as pd

from .indicadores import rsi_wilder

P = dict(lev=5.0, bullets=30, init=1, ACT=0.5, RES=0.5, PREF=0.25, RESTRIG=0.12, MMR=0.007, FEE=0.0006, SLIP=0.0005,
         rsi_th=15.0, ma_len=40)


class Balas:
    def __init__(self, db, capital_papel, real=None, avisar=None, clave="balas"):
        self.db = db; self.real = real; self.avisar = avisar or (lambda n, t: None); self.clave = clave
        st = db.get(clave)
        if st is None:
            st = dict(W=float(capital_papel), activo=False, rec=False, resd=False, used=0, ntn=0.0, inv=0.0, mbtc=0.0,
                      contrib=0.0, entry_t=None, dC=[], emaW=None, emaHist=[], regWeekly=False, levm=1.0, momT=False,
                      liqs=0, camps=0, ultima_vela=None, eq=float(capital_papel), dist_liq=None)
            db.set(clave, st)
        self.st = st

    # ---------------- utilidades
    def patrimonio(self, precio=None):
        s = self.st
        if not s["activo"]:
            return s["W"]
        C = precio or s.get("ultimo_precio")
        return s["W"] * (1 + (s["mbtc"] + s["inv"] - s["ntn"] / C) * C - s["contrib"])

    def _liq(self):
        s = self.st
        return s["ntn"] * (1 + P["MMR"]) / (s["mbtc"] + s["inv"]) if s["activo"] and (s["mbtc"] + s["inv"]) > 0 else None

    def _op(self, t, tipo, precio, nocional_usd, motivo):
        self.db.ejec("INSERT INTO operaciones (ts,cuenta,estrategia,lote,simbolo,lado,contratos,precio,nocional,comision,motivo,modo)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (int(pd.Timestamp(t).value // 10**6), "balas", "balas5", f"campaña_{self.st['camps']}", "XBTUSDM",
                      tipo, nocional_usd, precio, nocional_usd, abs(nocional_usd) * P["FEE"], motivo, "real" if self.real else "papel"))

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

    # ---------------- paso por vela cerrada
    def procesar(self, t, vela, cierres_4h, funding_8h=None, bloqueado=False):
        """t: cierre de la vela (=apertura de la siguiente). vela: (o,h,l,c) de la vela cerrada.
        cierres_4h: Serie de cierres 4h hasta esa vela (para RSI(2) y SMA40). funding_8h: tasa vigente."""
        s = self.st; t = pd.Timestamp(t)
        o, h, l, c = vela
        if s["ultima_vela"] and pd.Timestamp(s["ultima_vela"]) >= t:
            return
        self._calentar(t, cierres_4h)
        s["ultima_vela"] = str(t); s["ultimo_precio"] = c
        fr = (funding_8h if funding_8h is not None else 0.10 / 365 / 3) / 2      # por vela de 4h
        # 2) reserva, funding y liquidación durante la vela cerrada
        liq_now = False
        if s["activo"]:
            liq = self._liq()
            if not s["resd"] and o / liq - 1 <= P["RESTRIG"]:
                cash = P["RES"] * (1 - P["PREF"])
                s["mbtc"] += cash / o; s["contrib"] += cash; s["resd"] = True
                self.avisar("alta", "30 balas: entra la reserva (precio a menos de 12 % de la liquidación)")
                if self.real: self.real.aportar_margen(cash * s["W"], o)
            s["mbtc"] -= s["ntn"] * fr / o
            liq = self._liq()
            if l <= liq:
                s["W"] *= (1 - s["contrib"]); s["liqs"] += 1; liq_now = True
                self._op(t, "liquidacion", liq, s["ntn"] * s["W"], "liquidacion")
                self.avisar("critica", f"30 balas: LIQUIDACIÓN a {liq:.0f}")
                s.update(activo=False, ntn=0.0, inv=0.0, mbtc=0.0, contrib=0.0, used=0)
        s["eq"] = self.patrimonio(c)
        liq = self._liq()
        s["dist_liq"] = (c / liq - 1) if liq else None
        # 3) series diarias y semanales al cierre del día
        if t.hour == 0:
            self._dia(t, c)
        reg_today = s["regWeekly"]
        # 4) decisiones
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
        elif not liq_now and reg_today and rsi <= P["rsi_th"] and s["momT"] and not bloqueado:
            acciones.append(("entrar", s["levm"], above))
        # 1) ejecución en la apertura siguiente (= ahora, al precio de cierre de la vela)
        px = c
        for a in acciones:
            if a[0] == "salir" and s["activo"]:
                pe = px * (1 - P["SLIP"])
                pnl = (s["mbtc"] + s["inv"] - s["ntn"] / pe - s["ntn"] / pe * P["FEE"]) * pe - s["contrib"]
                self._op(t, "sell", pe, s["ntn"] * s["W"], "salida")
                if self.real: self.real.cerrar(pe)
                s["W"] *= (1 + pnl)
                self.avisar("op", f"30 balas: fin de campaña, resultado {pnl:+.1%} de la subcuenta")
                s.update(activo=False, ntn=0.0, inv=0.0, mbtc=0.0, contrib=0.0, used=0)
            elif a[0] == "entrar":
                usd = P["ACT"] * P["init"] / P["bullets"]
                s["ntn"] = usd * P["lev"] * a[1]
                s["inv"] = s["ntn"] / (px * (1 + P["SLIP"]))
                s["mbtc"] = usd / px - s["ntn"] * P["FEE"] / px + P["RES"] * P["PREF"] / px
                s["contrib"] = usd + P["RES"] * P["PREF"]
                s.update(resd=False, activo=True, entry_t=str(t), used=P["init"], rec=a[2])
                s["camps"] += 1
                self._op(t, "buy", px, s["ntn"] * s["W"], "entrada")
                if self.real: self.real.abrir(s["contrib"] * s["W"], s["ntn"] * s["W"], px)
                self.avisar("op", f"30 balas: nueva campaña a {px:.0f} (apalancamiento {P['lev'] * a[1]:.1f}x)")
            elif a[0] == "recargar" and s["activo"]:
                _, u, upos, lvm, add = a
                s["contrib"] += u
                if upos > 0:
                    q = upos * P["lev"] * lvm
                    s["ntn"] += q; s["inv"] += q / (px * (1 + P["SLIP"])); s["mbtc"] += upos / px - q * P["FEE"] / px
                    self._op(t, "buy", px, q * s["W"], f"recarga {add} balas")
                    if self.real: self.real.recargar(u * s["W"], q * s["W"], px)
                elif u > 0 and self.real:
                    self.real.aportar_margen(u * s["W"], px)        # recarga sólo de margen
                s["mbtc"] += (u - upos) / px
                s["used"] += add
        s["eq"] = self.patrimonio(px)
        liq = self._liq()
        s["dist_liq"] = (px / liq - 1) if liq else None
        self.db.set(self.clave, s)
        self.db.ejec("INSERT OR REPLACE INTO patrimonio VALUES (?,?,?,?,?,?)",
                     (int(t.value // 10**6), "balas", s["eq"], s["ntn"] * s["W"] if s["activo"] else 0, None, None))


    def forzar_salida(self, t, px, motivo="corte"):
        """Cierra la campaña ya (corte por caída o pedido manual)."""
        s = self.st
        if not s["activo"]:
            return
        pe = px * (1 - P["SLIP"])
        pnl = (s["mbtc"] + s["inv"] - s["ntn"] / pe - s["ntn"] / pe * P["FEE"]) * pe - s["contrib"]
        self._op(t, "sell", pe, s["ntn"] * s["W"], motivo)
        if self.real: self.real.cerrar(pe)
        s["W"] *= (1 + pnl)
        s.update(activo=False, ntn=0.0, inv=0.0, mbtc=0.0, contrib=0.0, used=0, eq=s["W"], dist_liq=None)
        self.db.set(self.clave, s)
        self.avisar("alta", f"30 balas: campaña cerrada por {motivo}, resultado {pnl:+.1%} de la subcuenta")


from .balas_real import EjecutorRealBalas  # noqa: E402,F401  (ejecución real, ver balas_real.py)
