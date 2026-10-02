"""Motor único de backtest: corre el código del servicio en vivo (motor, estrategias, asignador y 30 balas) vela por vela
contra el exchange simulado, con funding, costos por instrumento y las dos cuentas (protocolo §4).

    from validacion.motor_bt import Corrida
    r = Corrida(paquete, top50, resumen, universo, pesos={"rsi2_btc": 1.0}, modo="IS", corrida="A").correr()
    r.serie   # DataFrame cada 4 h: E, E_peor, E_main, E_bal, nocional por estrategia, funding acumulado
"""
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

from cascada import config as C
from cascada.balas import Balas
from cascada.bolsa import Papel
from cascada.datos import Datos
from cascada.db import Base
from cascada.estrategias import Mantener, TendenciaBTC, todas
from cascada.motor import Motor

from . import datos as D

H4 = pd.Timedelta(hours=4)
COMISION = 0.0006
DESLIZ = {"BTC": 0.0005, "ETH": 0.0005, "_": 0.0010}
PRIORIDAD = ["ab_cortos", "mom_alts", "balas5", "btc_tend", "rsi2_btc", "wr2", "sold_btc", "rsi2_eth", "hold_btc", "hold_eth"]


class PublicoBT:
    """Mercados y precios del backtest. El precio de ejecución en t es la apertura de la vela que empieza en t."""

    def __init__(self, M, mercados):
        self.m = mercados
        self.O = M["o"]; self.Cff = M["c"].ffill()
        self.t = None

    def mercados(self, refrescar=False):
        return self.m

    def precios(self, bases):
        t = self.t
        fila_o = self.O.loc[t] if t in self.O.index else None
        prev = t - H4
        fila_c = self.Cff.loc[prev] if prev in self.Cff.index else None
        out = {}
        for b in bases:
            px = fila_o.get(b) if fila_o is not None else None
            if px is None or px != px:
                px = fila_c.get(b) if fila_c is not None else None
            if px is not None and px == px:
                out[b] = float(px)
        return out

    def funding(self, base):
        return None


def mercados_de(corrida, columnas, contratos=None):
    if corrida == "A":       # estrategia pura: contratos fraccionarios
        return {s: dict(tam=1.0, minimo=1e-9, id=s) for s in columnas}
    out = {}
    for s in columnas:       # 3000 USDT con contratos reales de KuCoin (sin contrato hoy: 1 unidad)
        if contratos is not None and s in contratos.index and contratos.at[s, "tam"] == contratos.at[s, "tam"]:
            out[s] = dict(tam=float(contratos.at[s, "tam"]), minimo=float(contratos.at[s, "minimo"] or 1), id=s)
        else:
            out[s] = dict(tam=1.0, minimo=1.0, id=s)
    return out


class Resultado:
    def __init__(self, serie, db, info):
        self.serie = serie; self.db = db; self.info = info

    def operaciones(self):
        return pd.DataFrame(self.db.filas("SELECT * FROM lotes WHERE cuenta='principal' ORDER BY abierto_ts"))


class Corrida:
    def __init__(self, paquete, top50, resumen, universo, pesos, modo="IS", corrida="A", capital=None, desde=None,
                 hasta=None, ruta_db=":memory:", funding_xbt=None, log_cada=500):
        self.lim = D.limite(modo)
        self.desde = pd.Timestamp(desde or D.INICIO_IS)
        self.hasta = min(pd.Timestamp(hasta), self.lim) if hasta else self.lim
        self.pesos = dict(pesos); self.corrida = corrida
        self.capital = capital or (100_000.0 if corrida == "A" else 3000.0)
        self.paquete = paquete; self.archivos = (top50, resumen, universo)
        self.ruta_db = ruta_db; self.funding_xbt = funding_xbt; self.log_cada = log_cada

    def _cfg(self):
        cfg = C.Config(pesos=dict(self.pesos), prioridad=list(PRIORIDAD), capital_papel=self.capital,
                       corte_caida=9.0, alerta_caida=9.0, tope_con_balas_real=True, comision=COMISION)
        cfg.desactivadas = [k for k in PRIORIDAD if self.pesos.get(k, 0) <= 0]
        return cfg

    def correr(self):
        t0 = time.time()
        logging.disable(logging.WARNING)
        if self.ruta_db != ":memory:":
            Path(self.ruta_db).unlink(missing_ok=True)
        db = Base(self.ruta_db)
        bases, M = D.cargar_base(db, self.paquete, *self.archivos, hasta=self.hasta)
        F = self.paquete.funding(self.hasta)
        merc = mercados_de(self.corrida, bases, self.paquete.contratos())
        pub = PublicoBT(M, merc)
        cfg = self._cfg()
        datos = Datos(db, pub)
        w_bal = self.pesos.get("balas5", 0.0)
        papel = Papel(db, merc, capital=self.capital * (1 - w_bal), comision=COMISION, desliz=DESLIZ)
        balas = Balas(db, self.capital * w_bal) if w_bal > 0 else None
        motor = Motor(cfg, db, datos, papel, pub,
                      balas_patrimonio=lambda: balas.patrimonio() if balas else 0.0,
                      balas_plano=lambda: not (balas and balas.st.get("activo")),
                      balas_nocional=lambda: (balas.st["ntn"] * balas.st["W"]) if balas and balas.st.get("activo") else 0.0)
        est = {k: v for k, v in todas().items() if self.pesos.get(k, 0) > 0}
        if self.pesos.get("btc_tend", 0) > 0:
            est["btc_tend"] = TendenciaBTC()
        for b in ("BTC", "ETH"):
            if self.pesos.get(f"hold_{b.lower()}", 0) > 0:
                est[f"hold_{b.lower()}"] = Mantener(b)
        motor.est = est
        C_, H_, L_, O_ = M["c"], M["h"], M["l"], M["o"]
        fx = self.funding_xbt
        filas = []; pagado = 0.0
        ts = pd.date_range(self.desde, self.hasta - H4, freq="4h")
        for k, t in enumerate(ts):
            pub.t = t
            prev = t - H4
            vela = {}
            if prev in C_.index:
                rc = C_.loc[prev]          # vela incompleta: o/h/l faltantes toman el cierre
                rh, rl, ro = H_.loc[prev].fillna(rc), L_.loc[prev].fillna(rc), O_.loc[prev].fillna(rc)
            else:
                rc = rh = rl = ro = None
            # cierre vigente para valorar: último cierre conocido (una vela faltante no es una pérdida)
            ult = pub.Cff.loc[:prev].iloc[-1] if len(pub.Cff.loc[:prev]) else None
            # funding de los perpetuos durante la vela que cerró (cada evento con el precio de ese momento)
            if F is not None:
                for tf_, fila in F.loc[(F.index > prev) & (F.index <= t)].iterrows():
                    for b in list(papel.st["pos"]):
                        tasa = fila.get(b)
                        if tasa == tasa and tasa is not None:
                            cu = ult.get(b) if ult is not None else None
                            px_f = float(cu) if cu is not None and cu == cu else papel.st["pos"][b]["px"]
                            pagado += papel.aplicar_funding(b, float(tasa), px_f)
            # patrimonio al cierre y peor punto de la vela con las posiciones que hubo durante la vela
            E_main = E_peor_main = papel.st["caja"]
            for b, p in papel.st["pos"].items():
                tam = merc[b]["tam"]
                cu = ult.get(b) if ult is not None else None
                c = float(cu) if cu is not None and cu == cu else p["px"]
                tiene = rc is not None and rc.get(b) == rc.get(b) and rl.get(b) == rl.get(b)
                peor = (float(rl.get(b)) if p["c"] > 0 else float(rh.get(b))) if tiene else c
                topes = [s["px"] for s in papel.st["stops"].values() if s.get("estado", "abierta") == "abierta" and s["base"] == b]
                cubierto = sum(s["c"] for s in papel.st["stops"].values() if s.get("estado", "abierta") == "abierta" and s["base"] == b)
                if topes and cubierto >= abs(p["c"]) - 1e-9:
                    peor = max(peor, min(topes)) if p["c"] > 0 else min(peor, max(topes))
                E_main += p["c"] * tam * (c - p["px"])
                E_peor_main += p["c"] * tam * (peor - p["px"])
            E_bal = E_peor_bal = 0.0
            if balas:
                if rc is not None and rc.get("BTC") == rc.get("BTC"):
                    E_bal = balas.patrimonio(float(rc["BTC"])); E_peor_bal = balas.patrimonio(float(rl["BTC"]))
                else:
                    E_bal = E_peor_bal = balas.patrimonio()
            E = E_main + E_bal
            if E != E:
                raise RuntimeError(f"Patrimonio NaN en {t}: caja={papel.st['caja']} pos={papel.st['pos']}")
            noc = {}
            for L in motor.libro().values():
                cu = ult.get(L["simbolo"]) if ult is not None else None
                px = float(cu) if cu is not None and cu == cu else L["precio_entrada"]
                noc[L["estrategia"]] = noc.get(L["estrategia"], 0.0) + abs(L["contratos"]) * L["tam_contrato"] * px
            if balas and balas.st.get("activo") and rc is not None:
                noc["balas5"] = balas.st["ntn"] * balas.st["W"]
            filas.append(dict(t=t, E=E, E_peor=E_peor_main + E_peor_bal, E_main=E_main, E_bal=E_bal, funding=pagado,
                              **{f"n_{a}": v for a, v in noc.items()}))
            # 30 balas: capital asignado al empezar cada campaña (mientras está afuera sigue el peso sobre el total)
            if balas:
                if not balas.st["activo"] and E > 0:
                    objetivo = w_bal * E
                    papel.st["caja"] -= objetivo - balas.st["W"]
                    balas.st["W"] = objetivo; balas.st["eq"] = objetivo
                if rc is not None and rc.get("BTC") == rc.get("BTC"):
                    cierres = datos.v4("BTC", t, dias=60 if balas.st.get("calentado") else 420).c
                    fr = None
                    if fx is not None:
                        f0 = fx[fx.index <= t]
                        fr = float(f0.iat[-1]) if len(f0) else None
                    balas.procesar(t, (float(ro["BTC"]), float(rh["BTC"]), float(rl["BTC"]), float(rc["BTC"])), cierres,
                                   funding_8h=fr)
            # stops del papel con la vela que cerró, y ciclo del motor
            if rc is not None:
                for b in {L["simbolo"] for L in motor.libro().values()}:
                    if rc.get(b) == rc.get(b):
                        vela[b] = (float(ro.get(b)), float(rh.get(b)), float(rl.get(b)))
            motor.ciclo(t, velas_cerradas=vela)
            if self.log_cada and k % self.log_cada == 0:
                print(f"  {t} {time.time() - t0:5.0f} s  E={E:,.0f}", flush=True)
        serie = pd.DataFrame(filas).set_index("t").fillna({c: 0.0 for c in []})
        serie = serie.fillna(0.0)
        info = dict(segundos=round(time.time() - t0), desde=str(self.desde), hasta=str(self.hasta), corrida=self.corrida,
                    pesos=self.pesos, capital=self.capital, perp=self.paquete.perp)
        return Resultado(serie, db, info)


def metricas(serie, desde=None, hasta=None):
    """Tasa anual, caída (rango optimista/pesimista), Sharpe y Calmar de una serie de 4 h."""
    s = serie
    if desde is not None:
        s = s[s.index >= pd.Timestamp(desde)]
    if hasta is not None:
        s = s[s.index < pd.Timestamp(hasta)]
    E = s.E; Ep = s.E_peor
    años = (s.index[-1] - s.index[0]).total_seconds() / (365.25 * 86400)
    cagr = (E.iat[-1] / E.iat[0]) ** (1 / años) - 1 if años > 0 and E.iat[-1] > 0 else -1.0
    pico = E.cummax()
    dd_opt = float((E / pico - 1).min())
    dd_pes = float((Ep / pico.shift().fillna(E.iat[0]) - 1).clip(upper=0).min())
    rd = E.resample("D").last().pct_change().dropna()
    sharpe = float(rd.mean() / rd.std() * np.sqrt(365)) if rd.std() > 0 else 0.0
    return dict(cagr=float(cagr), dd_optimista=dd_opt, dd_pesimista=min(dd_pes, dd_opt), sharpe=sharpe,
                calmar=float(cagr / abs(min(dd_pes, dd_opt))) if dd_pes < 0 else None)
