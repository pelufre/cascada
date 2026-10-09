"""Motor del Sistema 3: arma la decisión de cada vela (diaria a las 00:00 UTC y de cortos cada 4 h) y la ejecuta en
KuCoin con la regla de conflicto y el movimiento de capital entre SPOT y FUTUROS de la especificación.

Decisión = lo que dicen las señales al cierre de la vela (modo, ventas, candidatas, salidas y entradas de cortos).
Ejecución = la decisión aplicada con los saldos del momento. Con `confirmar: true` la ejecución espera el OK.

Libro (estado "libro" en la base):
  alts:   {sim: dict(cant, costo, ts, reducida)}     costo = USDT pagados (con comisión) por la cantidad que queda
  btc:    dict(cant, costo, ts, reducida) | None
  cortos: {sim: dict(contratos, entrada, ts, reducida)}
  conflicto: bool
"""
import json
import logging
import math
import time
import uuid

import pandas as pd

from . import senales as S
from .fuentes import anchas, top50_cmc

log = logging.getLogger("sistema3.motor")
COMISION_SPOT = 0.001
POLVO_USD = 2.0                    # posiciones de menos de 2 USD se consideran cerradas


def ahora():
    return pd.Timestamp.now("UTC").tz_localize(None)


def ms(t):
    return int(pd.Timestamp(t).value // 10**6)


def pct(x, d=1):
    return "—" if x is None or x != x else f"{x * 100:+.{d}f} %".replace(".", ",")


def usd(x):
    return "—" if x is None else f"{x:,.0f}".replace(",", ".")


class Motor:
    def __init__(self, cfg, db, fuente, bolsa, avisar=None):
        self.cfg, self.db, self.fuente, self.bolsa = cfg, db, fuente, bolsa
        self.avisar = avisar or (lambda nivel, texto: log.info("[%s] %s", nivel, texto))

    # ============================================================================================ libro
    @property
    def libro(self):
        L = self.db.get("libro")
        if L is None:
            L = dict(alts={}, btc=None, cortos={}, conflicto=False)
        return L

    def guardar(self, L):
        self.db.set("libro", L)

    @staticmethod
    def hay_largos(L):
        return bool(L["alts"]) or bool(L["btc"])

    @staticmethod
    def hay_cortos(L):
        return bool(L["cortos"])

    # ============================================================================================ universo
    def actualizar_universo(self, t=None):
        """Top 50 de CMC: se baja una vez por semana (en el ciclo del lunes 00:00 UTC) y rige toda esa semana."""
        t = pd.Timestamp(t or ahora())
        lunes = (t.normalize() - pd.Timedelta(days=t.dayofweek)).strftime("%Y-%m-%d")
        if self.db.filas("SELECT 1 FROM universo WHERE fecha=? LIMIT 1", (lunes,)):
            return False
        lista = top50_cmc(self.cfg.cmc_api_key)
        with self.db.transaccion():
            for p, s in lista:
                self.db.ejec("INSERT OR REPLACE INTO universo VALUES (?,?,?)", (lunes, s, p))
        anterior = self.top50(t - pd.Timedelta(days=7))
        entran = [s for _, s in lista if s not in anterior]; salen = [s for s in anterior if s not in {x for _, x in lista}]
        if anterior and (entran or salen):
            self.avisar("info", f"Top 50 de la semana: entran {', '.join(entran) or '—'}; salen {', '.join(salen) or '—'}")
        return True

    def top50(self, t=None):
        t = pd.Timestamp(t or ahora())
        r = self.db.filas("SELECT MAX(fecha) f FROM universo WHERE fecha <= ?", (t.strftime("%Y-%m-%d"),))
        f = r[0]["f"] if r and r[0]["f"] else None
        if not f:
            return []
        return [x["simbolo"] for x in self.db.filas("SELECT simbolo FROM universo WHERE fecha=? ORDER BY puesto", (f,))]

    # ============================================================================================ decisión diaria (A)
    def _diarias(self, simbolos):
        dfs, errores = {}, []
        for s in sorted(set(simbolos)):
            try:
                d = self.fuente.diarias(s, 400)
                if d is not None and len(d):
                    dfs[s] = d
            except Exception as e:
                errores.append(f"{s}: {str(e)[:100]}")
        if errores:
            self.db.incidencia("media", "datos", "Sin velas diarias de Binance: " + "; ".join(errores)[:500])
        return dfs

    def decidir_diaria(self, t_cierre):
        """t_cierre: 00:00 UTC del día que empieza. La vela usada es la del día anterior (la última cerrada)."""
        dia = (pd.Timestamp(t_cierre).normalize() - pd.Timedelta(days=1))
        L = self.libro
        top = self.top50(t_cierre)
        if not top:
            raise RuntimeError("No hay top 50 guardado: falta CMC_API_KEY o falló CoinMarketCap")
        spot_bn = self.fuente.mercados_spot()
        # la amplitud usa todo el top 50 con par en Binance (como el backtest); para comprar, además, el par en KuCoin
        operables = {s for s in top if s in spot_bn and self.bolsa.mercado_spot(s) is not None}
        sims = (set(top) | set(L["alts"]) | {"BTC"}) & set(spot_bn)
        dfs = self._diarias(sims)
        if "BTC" not in dfs:
            raise RuntimeError("Sin velas diarias de BTC en Binance")
        C = anchas(dfs, "c"); H = anchas(dfs, "h"); Lo = anchas(dfs, "l"); V = anchas(dfs, "qv")
        C, H, Lo, V = (x[x.index <= dia] for x in (C, H, Lo, V))
        if C.index[-1] != dia:
            raise RuntimeError(f"La última vela diaria de BTC es {C.index[-1].date()}, falta la de {dia.date()}")
        # amplitud: elegibles del top 50 vigente (BTC y ETH incluidos); para operar, además, que exista en KuCoin
        E = S.elegibles(C, V, top)
        amp = S.amplitud(C, E)
        prev = self.db.get("amplitud_estado")
        if prev and pd.Timestamp(prev["fecha"]) == dia - pd.Timedelta(days=1):
            est = S.estado_amplitud(amp, prev["on"], dia)
        else:
            est = S.estado_amplitud(amp)
        amp_on = bool(est.iloc[-1])
        cb = C["BTC"]
        btc_ok = bool(S.filtro_btc(cb).iloc[-1])
        modo = S.modo_del_dia(btc_ok, amp_on)
        E_hoy = E.loc[dia] & pd.Series({s: s in operables for s in E.columns})
        r = S.fr20(C, H, Lo, cb, E_hoy, list(L["alts"]), riesgo_alt=self.cfg.riesgo_alt)
        info = dict(btc=float(cb.iloc[-1]), sma140=float(S.sma(cb, S.SMA_BTC).iloc[-1]), roc84=float(S.roc(cb, S.ROC_BTC).iloc[-1]),
                    amplitud=float(amp.iloc[-1]), amp_on=amp_on, btc_ok=btc_ok, n_elegibles=int(E.loc[dia].sum()))
        return dict(vela=str(dia.date()), modo=modo, info=info, ventas=r["ventas"], candidatas=r["candidatas"],
                    ranking=r["ranking"], amp_estado=dict(fecha=str(dia.date()), on=amp_on))

    def registrar_modo(self, A):
        i = A["info"]
        self.db.ejec("INSERT OR REPLACE INTO modos VALUES (?,?,?,?,?,?,?,?)",
                     (A["vela"], A["modo"], int(i["btc_ok"]), i["amplitud"], int(i["amp_on"]), i["btc"], i["sma140"], i["roc84"]))
        anterior = self.db.get("modo_actual")
        self.db.set("modo_actual", A["modo"])
        self.db.set("amplitud_estado", A["amp_estado"])
        if anterior and anterior != A["modo"]:
            self.avisar("alta", f"Cambio de modo: {anterior} → {A['modo']}")

    # ============================================================================================ decisión 4 h (B)
    def decidir_4h(self, t_cierre):
        """t_cierre: hora de cierre de la vela de 4 h (00, 04, …, 20 UTC)."""
        vela = pd.Timestamp(t_cierre) - pd.Timedelta(hours=4)
        L = self.libro
        top = self.top50(t_cierre)
        perp = self.fuente.mercados_perp()
        hoy_ms = ms(t_cierre)
        univ0 = [s for s in top if s != "BTC" and s in perp and self.bolsa.contrato(s) is not None
                 and hoy_ms - perp[s]["desde_ms"] >= S.MIN_DIAS_PERP * 86400_000]
        sims = set(univ0) | set(L["cortos"]) | {"BTC"}
        dfs, errores = {}, []
        for s in sorted(sims):
            if s not in perp:
                continue
            try:
                d = self.fuente.perp_4h(s, 1000)
                if d is not None and len(d):
                    dfs[s] = d[d.index <= vela]
            except Exception as e:
                errores.append(f"{s}: {str(e)[:100]}")
        if errores:
            self.db.incidencia("media", "datos", "Sin velas de 4 h de Binance: " + "; ".join(errores)[:500])
        if "BTC" not in dfs or dfs["BTC"].index[-1] != vela:
            raise RuntimeError(f"Falta la vela de 4 h de BTC que abre {vela}")
        O4 = anchas(dfs, "o"); C4 = anchas(dfs, "c"); QV = anchas(dfs, "qv")
        # liquidez: mediana de 30 días del volumen diario del perpetuo (días UTC completos) ≥ 2 M USD; ≥ 90 días de historia
        dia_vol = QV.groupby(QV.index.floor("D")).sum(min_count=6)
        completos = dia_vol.index < pd.Timestamp(t_cierre).normalize()
        medv = dia_vol[completos].iloc[-S.DIAS_VOL:].median()
        hist = C4.notna().sum()
        universo = [s for s in univ0 if s in C4.columns and medv.get(s, 0) >= S.MIN_VOL_USD and hist.get(s, 0) >= S.AB_ROC]
        reg, roc_btc = S.regimen_cortos(C4["BTC"])
        r = S.aberration(O4, C4, list(L["cortos"]), universo)
        return dict(vela=str(vela), regimen=reg, roc540_btc=roc_btc, salidas=r["salidas"], sin_precio=r["sin_precio"],
                    candidatas=r["candidatas"] if reg else [], universo=len(universo))

    # ============================================================================================ valuación
    def valuar(self, L=None):
        L = L or self.libro
        sims = list(L["alts"]) + (["BTC"] if L["btc"] else [])
        px = self.bolsa.precios_spot(sims) if sims else {}
        usdt = self.bolsa.saldos_spot().get("USDT", 0.0)
        largos = sum(p["cant"] * px.get(s, 0) for s, p in L["alts"].items()) + (L["btc"]["cant"] * px.get("BTC", 0) if L["btc"] else 0)
        fut = self.bolsa.patrimonio_futuros()
        nocional = 0.0
        for s, p in L["cortos"].items():
            try:
                nocional += abs(p["contratos"]) * self.bolsa.contrato(s)["tam"] * self.bolsa.precio_futuro(s)
            except Exception:
                nocional += abs(p["contratos"]) * p.get("tam", 1) * p["entrada"]
        T = usdt + largos + fut
        return dict(T=T, usdt=usdt, largos=largos, spot=usdt + largos, futuros=fut, nocional_cortos=nocional, precios=px)

    def registrar_patrimonio(self):
        L = self.libro
        v = self.valuar(L)
        self.db.ejec("INSERT OR REPLACE INTO patrimonio VALUES (?,?,?,?,?,?,?)",
                     (int(time.time() * 1000), v["T"], v["spot"], v["futuros"], v["precios"].get("BTC"),
                      v["largos"] / v["T"] if v["T"] else 0, v["nocional_cortos"] / v["T"] if v["T"] else 0))
        return v

    # ============================================================================================ conciliación
    def conciliar(self):
        """El libro no puede tener más de lo que hay en el exchange: si falta, se ajusta y queda una incidencia.
        Lo que haya de más en el exchange (aportes, otras monedas) no se toca."""
        L = self.libro
        saldos = self.bolsa.saldos_spot()
        cambios = []
        for s, p in list(L["alts"].items()) + ([("BTC", L["btc"])] if L["btc"] else []):
            real = saldos.get(s, 0.0)
            if real < p["cant"] * 0.995:
                cambios.append(f"{s}: libro {p['cant']:.6g}, cuenta {real:.6g}")
                if real <= 0:
                    (L["alts"].pop(s, None) if s != "BTC" else L.__setitem__("btc", None))
                else:
                    p["costo"] *= real / p["cant"]; p["cant"] = real
        pos = self.bolsa.posiciones_futuros()
        for s, p in list(L["cortos"].items()):
            real = -pos.get(s, 0.0)
            if real < p["contratos"]:
                cambios.append(f"corto {s}: libro {p['contratos']:g}, cuenta {real:g}")
                if real <= 0:
                    L["cortos"].pop(s)
                else:
                    p["contratos"] = real
        if cambios:
            self.guardar(L)
            self.db.incidencia("alta", "conciliacion", "Libro ajustado a la cuenta: " + "; ".join(cambios))
            self.avisar("alta", "Libro ajustado a lo que hay en KuCoin: " + "; ".join(cambios))
        return cambios

    # ============================================================================================ resumen previo
    def acciones_previstas(self, dec):
        """Lista legible de lo que haría la ejecución con el libro actual (para pedir confirmación)."""
        L = self.libro
        out = []
        A, B = dec.get("A"), dec.get("B")
        if A:
            m = A["modo"]
            if m == "USDT":
                if L["alts"] or L["btc"]:
                    out.append("Vender todo (modo USDT): " + ", ".join(list(L["alts"]) + (["BTC"] if L["btc"] else [])))
            elif m == "BTC":
                if L["alts"]:
                    out.append("Vender alts (modo BTC): " + ", ".join(L["alts"]))
                if not L["btc"]:
                    out.append(f"Comprar BTC por el {self.cfg.pct_btc * 100:.0f} % del capital" + (" (mitad por conflicto)" if L["cortos"] else ""))
            else:
                if L["btc"]:
                    out.append("Vender BTC (pasa a modo ALTS)")
                if A["ventas"]:
                    out.append("Vender: " + ", ".join(f"{s} (par alt/BTC bajo su SMA20)" for s in A["ventas"]))
                libres = S.CUPOS_ALTS - (len(L["alts"]) - len([s for s in A["ventas"] if s in L["alts"]]))
                c = [x for x in A["candidatas"] if x["sim"] not in L["alts"]][:max(libres, 0)]
                if c:
                    out.append("Comprar: " + ", ".join(f"{x['sim']} {x['peso'] * 100:.1f} %".replace(".", ",") for x in c)
                               + (" (mitad por conflicto)" if L["cortos"] else ""))
        if B:
            cerrar = [s for s in B["salidas"] + B["sin_precio"] if s in L["cortos"]]
            if cerrar:
                out.append("Cerrar cortos: " + ", ".join(cerrar))
            libres = S.CUPOS_CORTOS - (len(L["cortos"]) - len(cerrar))
            c = [x for x in B["candidatas"] if x["sim"] not in L["cortos"]][:max(libres, 0)]
            if c:
                medio = self.hay_largos(L) and not (A and A["modo"] == "USDT")
                out.append("Abrir cortos 10 % c/u: " + ", ".join(x["sim"] for x in c) + (" (mitad por conflicto)" if medio else ""))
        return out

    def texto_decision(self, dec):
        lin = []
        A, B = dec.get("A"), dec.get("B")
        if A:
            i = A["info"]
            lin.append(f"Modo {A['modo']} · BTC {usd(i['btc'])} (SMA140 {usd(i['sma140'])}, ROC84 {pct(i['roc84'])}) · "
                       f"amplitud {i['amplitud'] * 100:.0f} % {'encendida' if i['amp_on'] else 'apagada'}")
        if B:
            lin.append(f"Cortos: régimen {'ACTIVO' if B['regimen'] else 'apagado'} (ROC90 BTC {pct(B['roc540_btc'])})")
        acc = dec.get("acciones") or []
        lin += ["• " + a for a in acc] if acc else ["• Sin operaciones"]
        return "\n".join(lin)

    # ============================================================================================ ejecución
    def _op(self, parte, sim, lado, r, motivo):
        if not r:
            return
        self.db.ejec("INSERT INTO operaciones (ts,parte,simbolo,lado,cantidad,precio,usdt,comision,motivo,modo,orden) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (int(time.time() * 1000), parte, sim, lado, r.get("cantidad", r.get("contratos")), r["precio"],
                      r.get("usdt", r.get("nocional")), r.get("comision", 0), motivo, self.bolsa.modo, r.get("orden")))

    def _cerrada(self, parte, sim, lado, abierto, entrada, salida, invertido, pnl, motivo):
        self.db.ejec("INSERT INTO cerradas (parte,simbolo,lado,abierto_ts,cerrado_ts,entrada,salida,invertido,pnl,pnl_pct,motivo) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (parte, sim, lado, abierto, int(time.time() * 1000), entrada, salida, invertido, pnl,
                      pnl / invertido if invertido else None, motivo))

    # ---- largos
    def _vender_largo(self, L, sim, fraccion, motivo, informe):
        p = L["btc"] if sim == "BTC" else L["alts"].get(sim)
        if not p:
            return
        q = p["cant"] if fraccion >= 1 else p["cant"] * fraccion
        r = self.bolsa.vender_spot(sim, q)
        if r is None:
            if fraccion >= 1:                     # polvo: se da por cerrada
                (L.__setitem__("btc", None) if sim == "BTC" else L["alts"].pop(sim, None))
            self.guardar(L)
            return
        parte_costo = p["costo"] * min(r["cantidad"] / p["cant"], 1.0)
        pnl = r["usdt"] - parte_costo
        self._op("A", sim, "venta", r, motivo)
        entrada = parte_costo / r["cantidad"] if r["cantidad"] else None
        self._cerrada("A", sim, "largo", p["ts"], entrada, r["precio"], parte_costo, pnl, motivo)
        p["cant"] -= r["cantidad"]; p["costo"] -= parte_costo
        if p["cant"] * r["precio"] < POLVO_USD or fraccion >= 1:
            (L.__setitem__("btc", None) if sim == "BTC" else L["alts"].pop(sim, None))
        self.guardar(L)
        informe.append(f"Vendí {sim} {'la mitad' if fraccion < 1 else ''} a {r['precio']:.6g} · {pct(pnl / parte_costo if parte_costo else None)} ({motivo})".replace("  ", " "))

    def _comprar_largo(self, L, sim, usdt, motivo, informe, reducida=False):
        libre = self.bolsa.usdt_spot_libre()
        if usdt > libre * (1 - COMISION_SPOT) + 1e-9:
            return False
        r = self.bolsa.comprar_spot(sim, usdt)
        if not r:
            return False
        self._op("A", sim, "compra", r, motivo)
        costo = r["usdt"]
        if sim == "BTC":
            p = L["btc"] or dict(cant=0.0, costo=0.0, ts=int(time.time() * 1000), reducida=reducida)
            p["cant"] += r["cantidad"]; p["costo"] += costo; p["reducida"] = reducida
            L["btc"] = p
        else:
            p = L["alts"].get(sim) or dict(cant=0.0, costo=0.0, ts=int(time.time() * 1000), reducida=reducida)
            p["cant"] += r["cantidad"]; p["costo"] += costo; p["reducida"] = reducida
            L["alts"][sim] = p
        self.guardar(L)
        informe.append(f"Compré {sim} por {usd(r['usdt'])} USDT a {r['precio']:.6g} ({motivo})")
        return True

    # ---- cortos
    def _cerrar_corto(self, L, sim, contratos, motivo, informe):
        p = L["cortos"].get(sim)
        if not p:
            return
        n = int(min(contratos, p["contratos"]))
        r = self.bolsa.comprar_contratos(sim, n) if n >= 1 else None
        if r is None:
            if contratos >= p["contratos"]:
                L["cortos"].pop(sim)
                self.guardar(L)
            return
        tam = self.bolsa.contrato(sim)["tam"]
        invertido = r["contratos"] * tam * p["entrada"]
        pnl = r["contratos"] * tam * (p["entrada"] - r["precio"]) - r["comision"] - p.get("com_unit", 0) * r["contratos"]
        self._op("B", sim, "recompra", r, motivo)
        self._cerrada("B", sim, "corto", p["ts"], p["entrada"], r["precio"], invertido, pnl, motivo)
        p["contratos"] -= r["contratos"]
        if p["contratos"] < 1:
            L["cortos"].pop(sim)
        self.guardar(L)
        informe.append(f"Recompré {sim} ({r['contratos']:g} contratos) a {r['precio']:.6g} · {pct(pnl / invertido if invertido else None)} ({motivo})")

    def _margen(self, nocional_total, T):
        """Saldo de FUTUROS ≥ 60 % del nocional de cortos: si falta, pasa USDT libre de SPOT."""
        falta = self.cfg.margen_objetivo * nocional_total - self.bolsa.patrimonio_futuros()
        if falta > 1:
            disp = self.bolsa.usdt_spot_libre()
            m = min(falta, disp)
            if m >= 1:
                self.bolsa.transferir(m, "futuros")
            if m < falta - 1:
                self.db.incidencia("alta", "margen", f"Margen de futuros incompleto: faltan {falta - m:.0f} USDT")
                self.avisar("alta", f"Margen de futuros incompleto: faltan {falta - m:.0f} USDT y no hay USDT libre en SPOT")

    def _abrir_corto(self, L, sim, nocional, T, motivo, informe, reducida=False):
        c = self.bolsa.contrato(sim)
        px = self.bolsa.precio_futuro(sim)
        n = math.floor(nocional / (c["tam"] * px))
        if n < max(1, c["minimo"]):
            self.db.incidencia("media", "minimo", f"Corto {sim} salteado: {nocional:.0f} USDT no llega a un contrato")
            return False
        actual = sum(abs(p["contratos"]) * self.bolsa.contrato(s)["tam"] * px_ for s, p in L["cortos"].items()
                     for px_ in [self.bolsa.precio_futuro(s)])
        self._margen(actual + n * c["tam"] * px, T)
        self.bolsa.margen_cruzado(sim)
        r = self.bolsa.vender_contratos(sim, n)
        if not r:
            return False
        self._op("B", sim, "venta corto", r, motivo)
        p = L["cortos"].get(sim)
        if p:
            tot = p["contratos"] + r["contratos"]
            p["entrada"] = (p["entrada"] * p["contratos"] + r["precio"] * r["contratos"]) / tot
            p["contratos"] = tot
        else:
            L["cortos"][sim] = dict(contratos=r["contratos"], entrada=r["precio"], ts=int(time.time() * 1000),
                                    tam=c["tam"], com_unit=r["comision"] / r["contratos"] if r["contratos"] else 0)
        L["cortos"][sim]["reducida"] = reducida
        self.guardar(L)
        informe.append(f"Abrí corto {sim}: {r['contratos']:g} contratos a {r['precio']:.6g} ({usd(r['nocional'])} USDT, {motivo})")
        return True

    # ---- regla de conflicto
    def _mitad_todo(self, L, informe):
        informe.append("Conflicto largos/cortos: todo a la mitad")
        for s in list(L["alts"]):
            self._vender_largo(L, s, 0.5, "conflicto: mitad", informe)
            if s in L["alts"]:
                L["alts"][s]["reducida"] = True
        if L["btc"]:
            self._vender_largo(L, "BTC", 0.5, "conflicto: mitad", informe)
            if L["btc"]:
                L["btc"]["reducida"] = True
        for s in list(L["cortos"]):
            self._cerrar_corto(L, s, math.floor(L["cortos"][s]["contratos"] / 2), "conflicto: mitad", informe)
            if s in L["cortos"]:
                L["cortos"][s]["reducida"] = True
        L["conflicto"] = True
        self.guardar(L)

    def _fin_conflicto(self, L, informe):
        if not L["conflicto"] or (self.hay_largos(L) and self.hay_cortos(L)):
            return
        L["conflicto"] = False
        self.guardar(L)
        if not self.hay_largos(L) and not self.hay_cortos(L):
            return
        informe.append("Terminó el conflicto: las posiciones vuelven a su tamaño normal")
        if not self.hay_cortos(L):                    # el margen que queda en FUTUROS vuelve antes de reagrandar los largos
            libre = self.bolsa.libre_futuros()
            if libre >= 1:
                self.bolsa.transferir(libre, "spot")
        v = self.valuar(L); T = v["T"]
        if L["btc"]:
            falta = self.cfg.pct_btc * T - L["btc"]["cant"] * v["precios"].get("BTC", 0)
            if falta > POLVO_USD:
                self._comprar_largo(L, "BTC", min(falta, self.bolsa.usdt_spot_libre() * (1 - COMISION_SPOT)), "fin del conflicto", informe)
            if L["btc"]:
                L["btc"]["reducida"] = False
        for s, p in list(L["alts"].items()):
            if p.get("reducida"):
                monto = p["cant"] * v["precios"].get(s, 0)
                if monto > POLVO_USD and not self._comprar_largo(L, s, monto, "fin del conflicto", informe):
                    self.db.incidencia("media", "conflicto", f"No alcanzó el USDT para devolver {s} a su tamaño")
                if s in L["alts"]:
                    L["alts"][s]["reducida"] = False
        for s, p in list(L["cortos"].items()):
            c = self.bolsa.contrato(s); px = self.bolsa.precio_futuro(s)
            objetivo = math.floor(S.NOCIONAL_CORTO * T / (c["tam"] * px))
            if objetivo > p["contratos"]:
                self._abrir_corto(L, s, (objetivo - p["contratos"]) * c["tam"] * px, T, "fin del conflicto", informe)
            if s in L["cortos"]:
                L["cortos"][s]["reducida"] = False
        self.guardar(L)

    # ---- ejecución completa
    def ejecutar(self, dec):
        """Aplica la decisión con los saldos actuales. Orden: ventas (A), salidas de cortos (B), fin de conflicto, entradas de
        cortos (B), compras (A), devolución del margen sobrante. Devuelve el informe (lista de textos)."""
        informe = []
        self.bolsa.refrescar()
        self.bolsa.usdt_a_trading()
        self.conciliar()
        L = self.libro
        A, B = dec.get("A"), dec.get("B")
        # 1. ventas de la parte A
        if A:
            m = A["modo"]
            if m == "USDT":
                for s in list(L["alts"]):
                    self._vender_largo(L, s, 1, "modo USDT", informe)
                if L["btc"]:
                    self._vender_largo(L, "BTC", 1, "modo USDT", informe)
            elif m == "BTC":
                for s in list(L["alts"]):
                    self._vender_largo(L, s, 1, "modo BTC", informe)
            else:
                if L["btc"]:
                    self._vender_largo(L, "BTC", 1, "modo ALTS", informe)
                for s in A["ventas"]:
                    if s in L["alts"]:
                        self._vender_largo(L, s, 1, "par alt/BTC bajo SMA20", informe)
        # 2. salidas de cortos
        if B:
            for s in B["sin_precio"]:
                if s in L["cortos"]:
                    self._cerrar_corto(L, s, L["cortos"][s]["contratos"], "sin precio > 6 días", informe)
            for s in B["salidas"]:
                if s in L["cortos"]:
                    self._cerrar_corto(L, s, L["cortos"][s]["contratos"], "cierre sobre SMA120", informe)
        # 3. fin del conflicto
        self._fin_conflicto(L, informe)
        # 4. entradas de cortos
        if B and B["regimen"]:
            libres = S.CUPOS_CORTOS - len(L["cortos"])
            nuevas = [x for x in B["candidatas"] if x["sim"] not in L["cortos"]][:max(libres, 0)]
            if nuevas:
                if self.hay_largos(L) and not L["conflicto"]:
                    self._mitad_todo(L, informe)
                T = self.valuar(L)["T"]
                for x in nuevas:
                    f = 0.5 if L["conflicto"] else 1.0
                    self._abrir_corto(L, x["sim"], S.NOCIONAL_CORTO * T * f, T, "banda inferior Aberration", informe, reducida=f < 1)
        # 5. compras de la parte A
        if A:
            m = A["modo"]
            if m == "BTC" and not L["btc"]:
                if self.hay_cortos(L) and not L["conflicto"]:
                    self._mitad_todo(L, informe)
                T = self.valuar(L)["T"]
                f = 0.5 if L["conflicto"] else 1.0
                monto = min(self.cfg.pct_btc * T * f, self.bolsa.usdt_spot_libre() * (1 - COMISION_SPOT))
                if monto > POLVO_USD:
                    self._comprar_largo(L, "BTC", monto, "modo BTC", informe, reducida=f < 1)
            elif m == "ALTS":
                libres = S.CUPOS_ALTS - len(L["alts"])
                nuevas = [x for x in A["candidatas"] if x["sim"] not in L["alts"]][:max(libres, 0)]
                if nuevas:
                    if self.hay_cortos(L) and not L["conflicto"]:
                        self._mitad_todo(L, informe)
                    T = self.valuar(L)["T"]
                    f = 0.5 if L["conflicto"] else 1.0
                    for x in nuevas:
                        monto = x["peso"] * T * f
                        if not self._comprar_largo(L, x["sim"], monto, f"FR20 peso {x['peso'] * 100:.1f} %", informe, reducida=f < 1):
                            informe.append(f"Salteé {x['sim']}: no alcanza el USDT libre para {usd(monto)} USDT")
        # 6. margen: sin cortos, todo el saldo de FUTUROS vuelve a SPOT; con cortos, recarga si bajó del 40 %
        self._fin_conflicto(L, informe)
        if not self.hay_cortos(L):
            libre = self.bolsa.libre_futuros()
            if libre >= 1:
                self.bolsa.transferir(libre, "spot")
        else:
            v = self.valuar(L)
            if v["futuros"] < self.cfg.margen_alerta * v["nocional_cortos"]:
                self.avisar("alta", f"Saldo de FUTUROS {usd(v['futuros'])} USDT, bajo el 40 % del nocional de cortos ({usd(v['nocional_cortos'])})")
                self._margen(v["nocional_cortos"], v["T"])
        return informe

    def control_margen(self):
        """Cada 15 minutos: con cortos abiertos, si el saldo de FUTUROS cae bajo el 40 % del nocional, avisa y recarga."""
        L = self.libro
        if not self.hay_cortos(L):
            return
        v = self.valuar(L)
        if v["nocional_cortos"] and v["futuros"] < self.cfg.margen_alerta * v["nocional_cortos"]:
            self.avisar("alta", f"Saldo de FUTUROS {usd(v['futuros'])} USDT, bajo el 40 % del nocional de cortos: recargo desde SPOT")
            self._margen(v["nocional_cortos"], v["T"])

    # ============================================================================================ decisiones (confirmación)
    def nueva_decision(self, t_cierre, A=None, B=None, error=None):
        dec = dict(A=A, B=B)
        pend = self.pendiente()
        if pend:                                      # la parte A pendiente sigue valiendo hasta que venza
            viejo = pend["datos"]
            if not A and viejo.get("A"):
                dec["A"] = viejo["A"]
            self.db.ejec("UPDATE decisiones SET estado='reemplazada' WHERE id=?", (pend["id"],))
        dec["acciones"] = self.acciones_previstas(dec)
        diaria = bool(A) or bool(pend and pend["datos"].get("A"))
        vence = pd.Timestamp(t_cierre) + pd.Timedelta(hours=self.cfg.vence_diaria_h if diaria else self.cfg.vence_4h_h)
        if pend and pend["datos"].get("A") and not A:
            vence = min(vence, pd.to_datetime(pend["vence_ts"], unit="ms"))
        did = uuid.uuid4().hex[:4]
        self.db.ejec("INSERT INTO decisiones VALUES (?,?,?,?,?,?,?,?,?)",
                     (did, int(time.time() * 1000), str(t_cierre), "diaria" if diaria else "4h", "nueva",
                      self.texto_decision(dec), json.dumps(dec, default=str), ms(vence), None))
        return did, dec

    def pendiente(self):
        r = self.db.filas("SELECT * FROM decisiones WHERE estado='pendiente' ORDER BY ts DESC LIMIT 1")
        if not r:
            return None
        d = r[0]; d["datos"] = json.loads(d["datos"])
        if d["vence_ts"] < time.time() * 1000:
            self.db.ejec("UPDATE decisiones SET estado='vencida' WHERE id=?", (d["id"],))
            self.avisar("alta", f"Decisión #{d['id']} vencida sin respuesta: no se ejecutó")
            return None
        return d

    def marcar(self, did, estado, resultado=None):
        self.db.ejec("UPDATE decisiones SET estado=?, resultado=? WHERE id=?", (estado, resultado, did))

    def decision(self, did):
        r = self.db.filas("SELECT * FROM decisiones WHERE id=?", (did,))
        if not r:
            return None
        d = r[0]; d["datos"] = json.loads(d["datos"])
        return d
