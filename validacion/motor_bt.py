"""Motor único de backtest: corre el código del servicio en vivo (motor, estrategias, asignador y 30 balas) vela por vela
contra el exchange simulado, con funding, costos por instrumento y las dos cuentas (protocolo §4).

    from validacion.motor_bt import Corrida
    r = Corrida(paquete, top50, resumen, universo, pesos={"rsi2_btc": 1.0}, modo="IS", corrida="A").correr()
    r.serie   # DataFrame cada 4 h: E, E_peor, E_mejor, E_main, E_bal, nocional por estrategia, funding, exposición

Cada fila t es el patrimonio en el instante t (cierre de la vela [t − 4 h, t)). Orden de cada paso (enmienda E14):
  1. stops de esa vela: llenan al stop o a la apertura si la vela abrió más allá, con deslizamiento y comisión;
  2. funding liquidado en la vela, con la posición que quedó (un evento dentro de la vela con un stop en el medio se
     cobra a la posición que más paga: el orden dentro de la vela no se conoce);
  3. 30 balas: funding de XBTUSDM por evento y liquidación con el mínimo de la vela;
  4. valoración al cierre (E), peor punto (E_peor) y mejor punto (E_mejor) con las posiciones que hubo;
  5. decisiones de 30 balas y del motor, ejecutadas a la apertura de la vela siguiente.
La última fila es el cierre de la última vela del tramo; ahí no se decide nada.
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
    """Mercados y precios del backtest. El precio de ejecución en t es la apertura de la vela que empieza en t.
    apertura: {base: fecha de listado en KuCoin} (variante «operable en KuCoin»: antes de esa fecha no se opera)."""

    def __init__(self, M, mercados, apertura=None):
        self.m = mercados; self.apertura = apertura or {}
        self.O = M["o"]; self.Cff = M["c"].ffill()
        self.t = None

    def mercados(self, refrescar=False):
        if not self.apertura or self.t is None:
            return self.m
        return {b: v for b, v in self.m.items() if b not in self.apertura or self.apertura[b] <= self.t}

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

    def funding(self, base, simbolo=None):
        return None


def mercados_de(corrida, columnas, contratos=None, operable=False):
    """Contratos por símbolo. A: fraccionarios (la estrategia pura). B: tamaño y mínimo reales de KuCoin; sin contrato hoy,
    1 unidad (E5). operable=True: sólo los símbolos con contrato USDT-M en KuCoin con el mismo ticker (como los ve el
    servicio en vivo); devuelve también sus fechas de apertura."""
    out, apertura = {}, {}
    for s in columnas:
        hay = contratos is not None and s in contratos.index and contratos.at[s, "tam"] == contratos.at[s, "tam"]
        if operable and not hay:
            continue
        if corrida == "A":
            out[s] = dict(tam=1.0, minimo=1e-9, id=s)
        elif hay:
            out[s] = dict(tam=float(contratos.at[s, "tam"]), minimo=float(contratos.at[s, "minimo"] or 1), id=s)
        else:
            out[s] = dict(tam=1.0, minimo=1.0, id=s)
        if operable and "apertura" in contratos.columns and contratos.at[s, "apertura"] == contratos.at[s, "apertura"]:
            apertura[s] = pd.Timestamp(contratos.at[s, "apertura"]).tz_convert(None) if pd.Timestamp(
                contratos.at[s, "apertura"]).tzinfo else pd.Timestamp(contratos.at[s, "apertura"])
    return (out, apertura) if operable else out


class Resultado:
    def __init__(self, serie, db, info):
        self.serie = serie; self.db = db; self.info = info

    def operaciones(self):
        return pd.DataFrame(self.db.filas("SELECT * FROM lotes WHERE cuenta='principal' ORDER BY abierto_ts"))


class Corrida:
    def __init__(self, paquete, top50, resumen, universo, pesos, modo="IS", corrida="A", capital=None, desde=None,
                 hasta=None, ruta_db=":memory:", funding_xbt=None, log_cada=500, registro=False, cargar=None, mercados=None,
                 cfg=None, sin_costos=False, sin_funding=False, operable=False):
        """cargar(db) -> (bases, M, F) reemplaza al paquete (comparación con el papel); mercados: contratos a usar;
        cfg: configuración del servicio (alerta y corte incluidos) en lugar de la de validación.
        sin_costos / sin_funding: diagnóstico (las dos cuentas: comisiones, deslizamiento y funding de 30 balas también).
        operable: variante con el universo que de verdad opera KuCoin (contrato con el mismo ticker, desde su apertura)."""
        self.cargar = cargar; self.mercados = mercados; self.cfg_fija = cfg
        self.sin_costos = sin_costos; self.sin_funding = sin_funding; self.operable = operable
        if modo == "PAPEL" and cargar is None:
            raise PermissionError("El modo PAPEL compara con datos del servicio (cargar=…); no lee el paquete sin límite")
        self.lim = D.limite(modo)
        self.desde = pd.Timestamp(desde or D.INICIO_IS)
        self.hasta = min(pd.Timestamp(hasta), self.lim) if hasta else self.lim
        self.pesos = dict(pesos); self.corrida = corrida
        self.capital = capital or (100_000.0 if corrida == "A" else 3000.0)
        self.paquete = paquete; self.archivos = (top50, resumen, universo)
        self.ruta_db = ruta_db; self.funding_xbt = funding_xbt; self.log_cada = log_cada
        self.registro = registro      # P&L acumulado por lote y vela (para la cartera por lotes, enmienda E10)

    def _cfg(self):
        if self.cfg_fija is not None:
            return self.cfg_fija
        cfg = C.Config(pesos=dict(self.pesos), prioridad=list(PRIORIDAD), capital_papel=self.capital,
                       corte_caida=9.0, alerta_caida=9.0, tope_con_balas_real=True,
                       comision=0.0 if self.sin_costos else COMISION)
        cfg.desactivadas = [k for k in PRIORIDAD if self.pesos.get(k, 0) <= 0]
        return cfg

    def correr(self):
        t0 = time.time()
        logging.disable(logging.WARNING)
        if self.ruta_db != ":memory:":
            Path(self.ruta_db).unlink(missing_ok=True)
        db = Base(self.ruta_db)
        if self.cargar:
            bases, M, F = self.cargar(db)
        else:
            bases, M = D.cargar_base(db, self.paquete, *self.archivos, hasta=self.hasta)
            F = self.paquete.funding(self.hasta)
        apertura = None
        if self.mercados:
            merc = self.mercados
        elif self.operable:
            merc, apertura = mercados_de(self.corrida, bases, self.paquete.contratos(), operable=True)
        else:
            merc = mercados_de(self.corrida, bases, self.paquete.contratos())
        fx = self.funding_xbt
        if self.sin_funding:
            F = None; fx = None
        pub = PublicoBT(M, merc, apertura)
        cfg = self._cfg()
        datos = Datos(db, pub)
        w_bal = self.pesos.get("balas5", 0.0)
        papel = Papel(db, merc, capital=self.capital * (1 - w_bal), comision=0.0 if self.sin_costos else COMISION,
                      desliz=0.0 if self.sin_costos else DESLIZ)
        balas = Balas(db, self.capital * w_bal, costos=not self.sin_costos,
                      contrato_usd=1.0 if self.corrida == "B" else None) if w_bal > 0 else None
        motor = Motor(cfg, db, datos, papel, pub,
                      balas_patrimonio=lambda: balas.patrimonio() if balas else 0.0,
                      balas_plano=lambda: not (balas and balas.st.get("activo")),
                      balas_nocional=lambda: balas.nocional() if balas else 0.0)
        est = {k: v for k, v in todas().items() if self.pesos.get(k, 0) > 0}
        if self.pesos.get("btc_tend", 0) > 0:
            est["btc_tend"] = TendenciaBTC()
        for b in ("BTC", "ETH"):
            if self.pesos.get(f"hold_{b.lower()}", 0) > 0:
                est[f"hold_{b.lower()}"] = Mantener(b)
        motor.est = est
        C_, H_, L_, O_ = M["c"], M["h"], M["l"], M["o"]
        filas = []; pagado = 0.0; transferido = 0.0
        reg = []; fund_lote = {}; vistos = set()
        E_main0 = papel.st["caja"]; W0 = balas.st["W"] if balas else 0.0
        ts = pd.date_range(self.desde, self.hasta, freq="4h")
        for k, t in enumerate(ts):
            pub.t = t
            prev = t - H4
            if prev in C_.index:
                rc = C_.loc[prev]          # vela incompleta: o/h/l faltantes toman el cierre
                rh, rl, ro = H_.loc[prev].fillna(rc), L_.loc[prev].fillna(rc), O_.loc[prev].fillna(rc)
            else:
                rc = rh = rl = ro = None
            # cierre vigente para valorar: último cierre conocido (una vela faltante no es una pérdida)
            ult = pub.Cff.loc[:prev].iloc[-1] if len(pub.Cff.loc[:prev]) else None
            lib = motor.libro()

            def cierre(b, defecto):
                cu = ult.get(b) if ult is not None else None
                return float(cu) if cu is not None and cu == cu else defecto

            def tiene(b):
                return rc is not None and rc.get(b) == rc.get(b) and rl.get(b) == rl.get(b)

            # 1) stops de la vela que cerró, antes de valorar
            pre = {b: dict(p) for b, p in papel.st["pos"].items()}
            caja_pre = papel.st["caja"]
            vela = {b: (float(ro.get(b)), float(rh.get(b)), float(rl.get(b))) for b in pre if tiene(b)}
            hechos = papel.revisar_stops(vela) if vela else {}
            parados = {L["id"] for L in lib.values() if L.get("stop_orden") in hechos}
            # 2) funding de los perpetuos liquidado en la vela, con la posición que quedó
            if F is not None and (pre or papel.st["pos"]):
                evs = F.loc[(F.index > prev) & (F.index <= t)]
                if len(evs):
                    eventos = {}
                    for b in set(pre) | set(papel.st["pos"]):
                        if b in evs.columns:
                            col = evs[b].dropna()
                            if len(col):
                                eventos[b] = list(col.items())
                    pagos = papel.funding_vela(t, eventos, {b: p["c"] for b, p in pre.items()},
                                               {b: cierre(b, pre.get(b, papel.st["pos"].get(b, {})).get("px")) for b in eventos})
                    for b, pago in pagos.items():
                        pagado += pago
                        ls = [L for L in lib.values() if L["simbolo"] == b and L["id"] not in parados] or \
                             [L for L in lib.values() if L["simbolo"] == b]
                        tot = sum(abs(L["contratos"]) for L in ls)
                        for L in ls:
                            fund_lote[L["id"]] = fund_lote.get(L["id"], 0.0) + (pago * abs(L["contratos"]) / tot if tot else 0.0)
            # 3) 30 balas: funding de XBTUSDM por evento y liquidación durante la vela
            vela_btc = None
            mejor_bal = None
            if balas:
                if tiene("BTC"):
                    vela_btc = (float(ro["BTC"]), float(rh["BTC"]), float(rl["BTC"]), float(rc["BTC"]))
                    mejor_bal = balas.patrimonio(vela_btc[1])          # con la posición de antes: el máximo antes que el mínimo
                    ev_b = list(fx[(fx.index > prev) & (fx.index <= t)].items()) if fx is not None else []
                    balas.vela(t, vela_btc, ev_b)
            # 4) valoración: cierre con lo que quedó, peor punto (los parados en su llenado real) y mejor punto
            E_main = E_peor_main = papel.st["caja"]
            E_mejor_main = caja_pre
            c_sym, peor_sym, bruto = {}, {}, 0.0
            for b, p in papel.st["pos"].items():
                tam = merc[b]["tam"]
                c = cierre(b, p["px"])
                peor = (float(rl.get(b)) if p["c"] > 0 else float(rh.get(b))) if tiene(b) else c
                c_sym[b], peor_sym[b] = c, peor
                E_main += p["c"] * tam * (c - p["px"])
                E_peor_main += p["c"] * tam * (peor - p["px"])
                bruto += abs(p["c"]) * tam * c
            for b, p in pre.items():
                tam = merc[b]["tam"]
                c = cierre(b, p["px"])
                if tiene(b):
                    mejor = float(rh.get(b)) if p["c"] > 0 else float(rl.get(b))
                    q_par = p["c"] - papel.st["pos"].get(b, {}).get("c", 0.0)        # lo que cerró un stop
                    gap = any(h["base"] == b and h["gap"] for h in hechos.values())
                    E_mejor_main += (p["c"] - q_par) * tam * (mejor - p["px"]) + \
                        q_par * tam * ((float(ro.get(b)) if gap else mejor) - p["px"])
                else:
                    E_mejor_main += p["c"] * tam * (c - p["px"])
            E_bal = E_peor_bal = E_mejor_bal = 0.0
            if balas:
                if vela_btc:
                    E_bal = balas.patrimonio(vela_btc[3])
                    E_peor_bal = E_bal if (balas.st.get("liq_vela") or not balas.st["activo"]) else \
                        min(balas.patrimonio(vela_btc[2]), E_bal)
                    E_mejor_bal = max(mejor_bal, E_bal)
                else:
                    E_bal = E_peor_bal = E_mejor_bal = balas.patrimonio()
            E = E_main + E_bal
            if self.registro:
                ahora = set()
                for i, L in lib.items():
                    ent = L["precio_entrada"]; q = L["lado"] * L["contratos"] * L["tam_contrato"]
                    if i in parados:
                        px_s = hechos[L["stop_orden"]]["precio"]; c = pe = px_s
                    else:
                        c = c_sym.get(L["simbolo"], ent); pe = peor_sym.get(L["simbolo"], c)
                    f_ = fund_lote.get(i, 0.0); base = (L["pnl"] or 0.0) - f_
                    reg.append((k, i, L["estrategia"], base + q * (c - ent), base + q * (pe - ent),
                                0.0 if i in parados else abs(q) * c))
                    ahora.add(i)
                for i in vistos - ahora:          # cerrados en el ciclo anterior: P&L final
                    L = db.filas("SELECT * FROM lotes WHERE id=?", (i,))[0]
                    reg.append((k, i, L["estrategia"], (L["pnl"] or 0.0) - fund_lote.get(i, 0.0),
                                (L["pnl"] or 0.0) - fund_lote.get(i, 0.0), 0.0))
                vistos = ahora
            if E != E:
                raise RuntimeError(f"Patrimonio NaN en {t}: caja={papel.st['caja']} pos={papel.st['pos']}")
            noc = {}
            for L in lib.values():
                if L["id"] in parados:
                    continue
                px = cierre(L["simbolo"], L["precio_entrada"])
                noc[L["estrategia"]] = noc.get(L["estrategia"], 0.0) + abs(L["contratos"]) * L["tam_contrato"] * px
            n_bal = d_bal = 0.0
            if balas and balas.st.get("activo"):
                noc["balas5"] = n_bal = balas.nocional()
                d_bal = balas.delta_usd(vela_btc[3] if vela_btc else None)
            filas.append(dict(t=t, E=E, E_peor=E_peor_main + E_peor_bal, E_mejor=max(E_mejor_main + E_mejor_bal, E),
                              E_main=E_main, E_bal=E_bal, funding=pagado,
                              funding_balas=balas.st["funding_usd"] if balas else 0.0,
                              nocional_futuros=bruto + n_bal, exposicion_usd=bruto + d_bal,
                              **{f"n_{a}": v for a, v in noc.items()}))
            if k == len(ts) - 1:
                break                          # última fila: cierre del tramo, sin decisiones
            # 5) 30 balas: capital asignado al empezar cada campaña (E4) y decisión a la apertura siguiente
            if balas:
                if not balas.st["activo"] and E > 0:
                    objetivo = w_bal * E
                    transferido += objetivo - balas.st["W"]
                    papel.st["caja"] -= objetivo - balas.st["W"]
                    balas.st["W"] = objetivo; balas.st["eq"] = objetivo
                if vela_btc:
                    cierres = datos.v4("BTC", t, dias=60 if balas.st.get("calentado") else 420).c
                    px_b = pub.precios(["BTC"]).get("BTC", vela_btc[3])
                    balas.decidir(t, cierres, px=px_b)
            # 6) motor: decisión y órdenes a la apertura de la vela siguiente (los stops de esta vela ya corrieron)
            motor.ciclo(t, velas_cerradas={})
            if self.log_cada and k % self.log_cada == 0:
                print(f"  {t} {time.time() - t0:5.0f} s  E={E:,.0f}", flush=True)
        serie = pd.DataFrame(filas).set_index("t")
        serie = serie.fillna(0.0)
        info = dict(segundos=round(time.time() - t0), desde=str(self.desde), hasta=str(self.hasta), corrida=self.corrida,
                    pesos=self.pesos, capital=self.capital, perp=getattr(self.paquete, "perp", None),
                    operable=self.operable, sin_costos=self.sin_costos, sin_funding=self.sin_funding,
                    funding_por_lote=fund_lote, campañas_balas=list(balas.st["campanas"]) if balas else [])
        info["atribucion"] = self._atribucion(db, papel, balas, merc, serie, fund_lote, pagado, transferido, E_main0, W0,
                                              pub, vela_btc)
        res = Resultado(serie, db, info)
        if self.registro:
            res.lotes_vela = pd.DataFrame(reg, columns=["k", "lote", "estrategia", "cum", "cum_peor", "noc"])
            res.lotes_vela["k"] = res.lotes_vela["k"].astype(int)
            res.lotes_vela["t"] = serie.index[res.lotes_vela.k.values]
        return res

    @staticmethod
    def _atribucion(db, papel, balas, merc, serie, fund_lote, pagado, transferido, E_main0, W0, pub, vela_btc):
        """Resultado por estrategia que cierra con el patrimonio (V09): lotes de la principal (cerrados y abiertos al
        último cierre, netos de comisiones y del funding que pagó cada lote) y campañas de 30 balas (cerradas y la
        abierta). Residuo = patrimonio final − inicial − suma de las partes (debe ser ~0)."""
        lotes = db.filas("SELECT * FROM lotes WHERE cuenta='principal'")
        Cff = pub.Cff
        ult = Cff.iloc[-1] if len(Cff) else None
        por, abiertos, cerrados = {}, {}, {}
        for L in lotes:
            r = (L["pnl"] or 0.0) - fund_lote.get(L["id"], 0.0)
            if L["cerrado_ts"] is None:
                c = float(ult.get(L["simbolo"])) if ult is not None and ult.get(L["simbolo"]) == ult.get(L["simbolo"]) \
                    else L["precio_entrada"]
                r += L["lado"] * abs(L["contratos"]) * L["tam_contrato"] * (c - L["precio_entrada"])
                abiertos[L["estrategia"]] = abiertos.get(L["estrategia"], 0.0) + r
            else:
                cerrados[L["estrategia"]] = cerrados.get(L["estrategia"], 0.0) + r
            por[L["estrategia"]] = por.get(L["estrategia"], 0.0) + r
        out = dict(por_estrategia=por, lotes_cerrados=cerrados, lotes_abiertos=abiertos, funding_principal=pagado,
                   comisiones_principal=float(db.filas("SELECT COALESCE(SUM(comision),0) s FROM operaciones "
                                                       "WHERE cuenta='principal'")[0]["s"]),
                   transferido_a_balas=transferido)
        E_main_fin = float(serie.E_main.iat[-1]); E_bal_fin = float(serie.E_bal.iat[-1])
        out["residuo_principal"] = (E_main_fin - E_main0 + transferido) - sum(por.values())
        if balas:
            st = balas.st
            abierta = (balas.patrimonio(vela_btc[3] if vela_btc else None) - st["W"]) if st["activo"] else 0.0
            out["balas"] = dict(campañas_cerradas=st["resultado_usd"], campaña_abierta=abierta, funding=st["funding_usd"],
                                comisiones=st["comisiones_usd"], campañas=st["camps"], liquidaciones=st["liqs"])
            por["balas5"] = st["resultado_usd"] + abierta
            out["residuo_balas"] = (E_bal_fin - W0 - transferido) - por["balas5"]
        out["total"] = sum(por.values())
        return out


def metricas(serie, desde=None, hasta=None):
    """Tasa anual, caída, Sharpe y Calmar de una serie de 4 h (fila t = patrimonio en el instante t; el tramo incluye la
    fila de `hasta`, que es el cierre de su última vela). La caída se informa en tres medidas:
      optimista: sólo cierres de 4 h (máximo de cierres → cierre más bajo);
      pesimista: peor punto de cada vela contra el máximo de los cierres anteriores;
      estricta:  peor punto de cada vela contra el máximo de los MEJORES puntos de las velas anteriores y de la misma
                 vela (se supone que dentro de la vela el máximo vino antes que el mínimo: el orden más desfavorable).
    Los stops ya están liquidados a su precio real (apertura si hubo salto) antes de valorar cada vela (E14). Con velas de
    4 h el orden real dentro de la vela no se conoce: la caída verdadera está entre optimista y estricta."""
    s = serie
    if desde is not None:
        s = s[s.index >= pd.Timestamp(desde)]
    if hasta is not None:
        s = s[s.index <= pd.Timestamp(hasta)]
    E = s.E; Ep = s.E_peor
    años = (s.index[-1] - s.index[0]).total_seconds() / (365.25 * 86400)
    cagr = (E.iat[-1] / E.iat[0]) ** (1 / años) - 1 if años > 0 and E.iat[-1] > 0 else -1.0
    pico = E.cummax()
    dd_opt = float((E / pico - 1).min())
    dd_pes = float((Ep / pico.shift().fillna(E.iat[0]) - 1).clip(upper=0).min())
    dd_est = None
    if "E_mejor" in s:
        pico_m = np.maximum(s.E_mejor.cummax(), pico)
        dd_est = float(min((Ep / pico_m - 1).clip(upper=0).min(), dd_pes, dd_opt))
    rd = E.resample("D").last().pct_change().dropna()
    sharpe = float(rd.mean() / rd.std() * np.sqrt(365)) if rd.std() > 0 else 0.0
    out = dict(cagr=float(cagr), dd_optimista=dd_opt, dd_pesimista=min(dd_pes, dd_opt), dd_estricta=dd_est, sharpe=sharpe,
               calmar=float(cagr / abs(min(dd_pes, dd_opt))) if dd_pes < 0 else None)
    if "nocional_futuros" in s:
        out["nocional_max"] = float((s.nocional_futuros / s.E).max())
        out["exposicion_max"] = float((s.exposicion_usd / s.E).max())
    return out


def por_año(serie):
    """Retorno de cada año calendario: del patrimonio al 1/1 00:00 (o al inicio del tramo) al del 1/1 siguiente (o al
    final del tramo). La fila t es el patrimonio en el instante t, así que la del 1/1 00:00 cierra el año anterior."""
    E = serie.E
    out = {}
    for y in range(E.index[0].year, E.index[-1].year + 1):
        ini = max(pd.Timestamp(f"{y}-01-01"), E.index[0]); fin = min(pd.Timestamp(f"{y + 1}-01-01"), E.index[-1])
        if fin <= ini:
            continue
        out[str(y)] = float(E[E.index <= fin].iat[-1] / E[E.index <= ini].iat[-1] - 1)
    return out
