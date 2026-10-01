"""Motor de la cuenta principal: un ciclo por cada cierre de vela de 4h (00, 04, 08, 12, 16, 20 UTC)."""
import hashlib
import json
import logging
import time

import pandas as pd

from . import riesgo
from .asignador import a_contratos, asignar
from .estrategias import todas

log = logging.getLogger("cascada")


def _oid(*partes):
    return hashlib.sha1("|".join(map(str, partes)).encode()).hexdigest()[:32]


def _ms(t):
    return int(pd.Timestamp(t).value // 10**6)


class Motor:
    def __init__(self, cfg, db, datos, bolsa, publico, avisar=None, balas_patrimonio=None):
        self.cfg = cfg; self.db = db; self.datos = datos; self.bolsa = bolsa; self.pub = publico
        self.avisar = avisar or (lambda nivel, texto: None)
        self.balas_patrimonio = balas_patrimonio or (lambda: 0.0)
        self.est = todas()

    # ------------------------------------------------------------ libro
    def libro(self):
        return {r["id"]: r for r in self.db.filas("SELECT * FROM lotes WHERE cerrado_ts IS NULL AND cuenta='principal'")}

    def _registrar_op(self, t, est, lote, sim, lado, contratos, res, motivo):
        tam = self.mercados[sim]["tam"]
        self.db.ejec("INSERT INTO operaciones (ts,cuenta,estrategia,lote,simbolo,lado,contratos,precio,nocional,comision,motivo,modo,client_oid,orden_id)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (_ms(t), "principal", est, lote, sim, lado, contratos, res["precio"], contratos * tam * res["precio"],
                      res.get("comision", 0), motivo, self.bolsa.modo, res.get("id"), res.get("id")))

    def _cerrar_lote(self, t, L, motivo, precio=None):
        sim = L["simbolo"]; c = abs(L["contratos"])
        if L.get("stop_orden"):
            self.bolsa.cancelar_stop(sim, L["stop_orden"])
        if precio is None:
            lado = "sell" if L["lado"] > 0 else "buy"
            res = self.bolsa.orden_mercado(sim, lado, c, False, _oid(L["id"], "cierre", t))
            self._registrar_op(t, L["estrategia"], L["id"], sim, lado, c, res, motivo)
            precio = res["precio"]
        pnl = L["lado"] * c * L["tam_contrato"] * (precio - L["precio_entrada"])
        self.db.ejec("UPDATE lotes SET cerrado_ts=?, precio_salida=?, motivo_salida=?, pnl=? WHERE id=?",
                     (_ms(t), precio, motivo, pnl, L["id"]))

    # ------------------------------------------------------------ ciclo
    def ciclo(self, t, precios=None, velas_cerradas=None):
        """t: momento de decisión (cierre de vela). precios: base -> último precio. velas_cerradas: base -> (o,h,l)."""
        t = pd.Timestamp(t)
        cfg = self.cfg
        self.mercados = self.pub.mercados()
        universo = [s for s in self.datos.universo(t) if s in self.mercados]
        lib = self.libro()
        bases = sorted(set(universo) | {"BTC", "ETH"} | {L["simbolo"] for L in lib.values()})
        if precios is None:
            precios = self.pub.precios([b for b in bases if b in self.mercados])
        if self.bolsa.modo == "papel":
            self.bolsa.fijar_precios(precios)
            if velas_cerradas:
                self.bolsa.revisar_stops(velas_cerradas)

        # 1) stops que se ejecutaron en el exchange
        abiertos_stop = self.bolsa.stops_abiertos({L["simbolo"] for L in lib.values() if L.get("stop_orden")})
        for L in list(lib.values()):
            if L.get("stop_orden") and L["stop_orden"] not in abiertos_stop:
                px = self.bolsa.precio_stop(L["simbolo"], L["stop_orden"], L["abierto_ts"]) or L["stop"]
                self._cerrar_lote(t, dict(L, stop_orden=None), "stop", precio=px)
                self.db.ejec("INSERT INTO operaciones (ts,cuenta,estrategia,lote,simbolo,lado,contratos,precio,nocional,comision,motivo,modo)"
                             " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (_ms(t), "principal", L["estrategia"], L["id"], L["simbolo"], "sell" if L["lado"] > 0 else "buy",
                              abs(L["contratos"]), px, abs(L["contratos"]) * L["tam_contrato"] * px, 0, "stop", self.bolsa.modo))
                self.avisar("info", f"Stop ejecutado: {L['estrategia']} {L['simbolo']} a {px:.6g}")
        lib = self.libro()

        # 2) patrimonio y riesgo
        E_main = self.bolsa.patrimonio()
        E_bal = self.balas_patrimonio()
        E = E_main + E_bal
        estado_riesgo = riesgo.actualizar_patrimonio(self.db, t, E, E_main, E_bal, lib, precios, cfg)
        if estado_riesgo["corte"] and not self.db.get("bloqueado"):
            self.db.set("bloqueado", True)
            self.db.incidencia("critica", "corte_caida", f"Caída {estado_riesgo['caida']:.1%} ≥ corte {cfg.corte_caida:.0%}: se cierra todo")
            self.avisar("critica", f"CORTE POR CAÍDA: {estado_riesgo['caida']:.1%}. Cierro todo y bloqueo entradas. Reactivar con /reanudar.")
            for L in lib.values():
                self._cerrar_lote(t, L, "corte")
            return dict(t=str(t), corte=True)
        if estado_riesgo["alerta"]:
            if self.db.incidencia("alta", "alerta_caida", f"Caída desde el máximo {estado_riesgo['caida']:.1%} ≥ alerta {cfg.alerta_caida:.0%}"):
                self.avisar("alta", f"Alerta: caída desde el máximo {estado_riesgo['caida']:.1%}")
        else:
            self.db.resolver("alerta_caida")

        # 3) decisiones de cada estrategia
        deseos = {}
        abiertos_por_est = {}
        for L in lib.values():
            abiertos_por_est.setdefault(L["estrategia"], {})[L["id"]] = L
        for nombre, e in self.est.items():
            ab = abiertos_por_est.get(nombre, {})
            if not e.decide_en(t) or cfg.pesos.get(nombre, 0) <= 0 and not ab:
                deseos[nombre] = [dict(id=i, simbolo=L["simbolo"], lado=L["lado"], frac=json.loads(L["meta"] or "{}").get("frac", 0),
                                       reescalable=bool(L["reescalable"]), stop=L["stop"]) for i, L in ab.items()] \
                    if cfg.pesos.get(nombre, 0) > 0 else []
                continue
            st = self.db.get(f"est_{nombre}", {})
            st["_cerrados"] = [r["id"] for r in self.db.filas(
                "SELECT id FROM lotes WHERE estrategia=? AND cerrado_ts IS NOT NULL AND cerrado_ts > ?",
                (nombre, _ms(t - pd.Timedelta(days=120))))]
            try:
                deseos[nombre] = e.paso(t, self.datos, st, ab, universo, self.mercados)
                st.pop("_cerrados", None)
            except Exception as ex:   # una estrategia con error no frena a las demás: mantiene lo que tiene
                log.exception("estrategia %s", nombre)
                self.db.incidencia("alta", f"error_{nombre}", f"{nombre}: {ex}"[:300])
                deseos[nombre] = [dict(id=i, simbolo=L["simbolo"], lado=L["lado"], frac=json.loads(L["meta"] or "{}").get("frac", 0),
                                       reescalable=bool(L["reescalable"]), stop=L["stop"]) for i, L in ab.items()]
                continue
            self.db.set(f"est_{nombre}", st)
        # nunca reabrir un lote ya cerrado (el id identifica una entrada)
        cerr_ids = {r["id"] for r in self.db.filas("SELECT id FROM lotes WHERE cerrado_ts IS NOT NULL AND cerrado_ts > ?",
                                                      (_ms(t - pd.Timedelta(days=120)),))}
        for n in deseos:
            deseos[n] = [L for L in deseos[n] if L["id"] not in cerr_ids]
        if self.db.get("bloqueado") or self.db.get("pausado") or not self.db.get("conciliacion_ok", True):
            # en pausa no se abren lotes nuevos; los abiertos siguen su curso (salidas y stops)
            for n in deseos:
                deseos[n] = [L for L in deseos[n] if L["id"] in lib]

        # 4) asignación
        w_bal = cfg.pesos.get("balas5", 0.0)
        presupuesto = cfg.tope_nocional * E - w_bal * E
        prio = [p for p in cfg.prioridad if p != "balas5"]
        objetivos, resumen = asignar(E, presupuesto, cfg.pesos, prio, deseos, lib, precios, self.mercados)
        for est, r in resumen.items():
            self.db.ejec("INSERT OR REPLACE INTO asignacion VALUES (?,?,?,?,?)", (_ms(t), est, r["pedido"], r["concedido"], r["g"]))

        # 5) órdenes: primero cierres y reducciones, después aperturas y aumentos
        deseados = {L["id"]: (est, L) for est, ls in deseos.items() for L in ls}
        for i, L in lib.items():
            if i not in deseados:
                self._cerrar_lote(t, L, "salida")
        lib = self.libro()
        aumentos = []
        bajo_minimo = []
        for i, (est, L) in deseados.items():
            sim = L["simbolo"]
            if sim not in self.mercados or sim not in precios:
                continue
            m = self.mercados[sim]; px = precios[sim]
            ab = lib.get(i)
            if ab is None:
                c = a_contratos(objetivos.get(i), px, m)
                if c <= 0:
                    if objetivos.get(i, 0) and objetivos[i] > 0:
                        bajo_minimo.append(f"{est}:{sim}")
                    continue
                aumentos.append(("abrir", est, L, c))
                continue
            # lote existente
            if L.get("reescalable") and objetivos.get(i) is not None:
                c = a_contratos(objetivos[i], px, m)
                actual = abs(ab["contratos"])
                dif_nocional = abs(c - actual) * m["tam"] * px
                if dif_nocional > max(cfg.reajuste_fraccion * actual * m["tam"] * px, cfg.reajuste_usdt):
                    if c < actual:
                        self._ajustar(t, ab, c)
                    else:
                        aumentos.append(("aumentar", est, L, c))
            nuevo_stop = L.get("stop")
            if nuevo_stop and ab.get("stop") and abs(nuevo_stop - ab["stop"]) / ab["stop"] > 1e-6:
                self._mover_stop(t, ab, nuevo_stop)
        for accion, est, L, c in aumentos:
            if accion == "abrir":
                self._abrir(t, est, L, c)
            else:
                self._ajustar(t, lib[L["id"]], c)
        if bajo_minimo:
            self.db.incidencia("media", "bajo_minimo", "Lotes bajo el contrato mínimo, no abiertos: " + ", ".join(sorted(set(bajo_minimo))))
        else:
            self.db.resolver("bajo_minimo")

        # 6) tope de nocional y conciliación
        lib = self.libro()
        riesgo.controlar_tope(self, t, E, lib, precios)
        riesgo.conciliar(self, lib)
        self.db.set("ultimo_ciclo", dict(t=str(t), ts=time.time(), E=E, E_main=E_main, E_bal=E_bal))
        return dict(t=str(t), E=E, resumen=resumen)

    # ------------------------------------------------------------ acciones
    def _abrir(self, t, est, L, c):
        sim = L["simbolo"]; lado = "buy" if L["lado"] > 0 else "sell"
        res = self.bolsa.orden_mercado(sim, lado, c, False, _oid(L["id"], "abrir", t))
        self._registrar_op(t, est, L["id"], sim, lado, c, res, "entrada")
        stop = None; stop_oid = None
        if L.get("stop_dist"):
            stop = res["precio"] - L["lado"] * L["stop_dist"]
            stop_oid = self.bolsa.orden_stop(sim, "sell" if L["lado"] > 0 else "buy", c, stop, _oid(L["id"], "stop", stop))
        self.db.ejec("INSERT INTO lotes (id,cuenta,estrategia,simbolo,lado,contratos,tam_contrato,precio_entrada,stop,stop_orden,"
                     "reescalable,abierto_ts,meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (L["id"], "principal", est, sim, L["lado"], c, self.mercados[sim]["tam"], res["precio"], stop, stop_oid,
                      int(bool(L.get("reescalable"))), _ms(t), json.dumps(dict(frac=L["frac"]))))
        self.avisar("op", f"Entrada {est}: {'largo' if L['lado'] > 0 else 'corto'} {sim} {c:g} contratos a {res['precio']:.6g}")

    def _ajustar(self, t, ab, c):
        sim = ab["simbolo"]; actual = abs(ab["contratos"]); d = c - actual
        if d == 0:
            return
        lado = ("buy" if ab["lado"] > 0 else "sell") if d > 0 else ("sell" if ab["lado"] > 0 else "buy")
        res = self.bolsa.orden_mercado(sim, lado, abs(d), False, _oid(ab["id"], "ajuste", c, t))
        self._registrar_op(t, ab["estrategia"], ab["id"], sim, lado, abs(d), res, "reajuste")
        if d > 0:
            px = (actual * ab["precio_entrada"] + abs(d) * res["precio"]) / c
        else:
            px = ab["precio_entrada"]
            self.db.ejec("UPDATE lotes SET pnl=COALESCE(pnl,0)+? WHERE id=?",
                         (ab["lado"] * abs(d) * ab["tam_contrato"] * (res["precio"] - ab["precio_entrada"]), ab["id"]))
        self.db.ejec("UPDATE lotes SET contratos=?, precio_entrada=? WHERE id=?", (c, px, ab["id"]))
        if ab.get("stop_orden"):
            self._mover_stop(t, dict(ab, contratos=c), ab["stop"])

    def _mover_stop(self, t, ab, stop):
        if ab.get("stop_orden"):
            self.bolsa.cancelar_stop(ab["simbolo"], ab["stop_orden"])
        oid = self.bolsa.orden_stop(ab["simbolo"], "sell" if ab["lado"] > 0 else "buy", abs(ab["contratos"]), stop,
                                    _oid(ab["id"], "stop", stop))
        self.db.ejec("UPDATE lotes SET stop=?, stop_orden=? WHERE id=?", (stop, oid, ab["id"]))

    def cerrar_todo(self, t, motivo="manual"):
        for L in self.libro().values():
            self._cerrar_lote(t, L, motivo)
