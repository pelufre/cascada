"""Motor de la cuenta principal: un ciclo por cada cierre de vela de 4h (00, 04, 08, 12, 16, 20 UTC).

Una sola cuenta con posición neta por símbolo. El libro guarda lotes por estrategia; en un símbolo todos los lotes abiertos
van del mismo lado (un lote nuevo del lado contrario se omite mientras haya otro abierto), así la posición del exchange es
la suma de los lotes, toda reducción va reduce-only y cada lote tiene su propio stop reduce-only.

Orden del ciclo:
  1. órdenes no aplicadas del todo (aplicado=0, en cualquier estado: enviando, incierta, abierta, o terminal con la
     aplicación interrumpida): se consultan por clientOid y se aplica lo que llenó y falta en el libro
  2. stops: ejecutado → se cierra el lote al precio real; cancelado → se repone (nunca se toma un stop ausente por ejecutado)
  3. conciliación ANTES de operar; si libro y exchange no coinciden, ese ciclo sólo se reduce
  4. patrimonio, alerta y corte; el corte deja el estado «liquidando» y se reintenta hasta quedar plano
  5. estrategias → asignación en cascada → contratos objetivo → recorte por tope sobre los objetivos
  6. en pausa, bloqueo, desconciliación u orden pendiente en el símbolo: sólo reducciones
  7. una orden neta por símbolo y un solo lado por símbolo (si entran pedidos de los dos lados sobre un símbolo plano,
     entra el de mayor prioridad); el libro registra lo que llenó de verdad (parciales incluidos), en una sola
     transacción con el estado de la orden; lo que se compensa entre lotes del mismo símbolo pasa de uno a otro al precio
     de mercado, sin orden. Una orden que el exchange sigue mostrando abierta nunca se da por aplicada
  8. stops de cada lote; si no se puede poner, el lote se cierra
  9. conciliación final
"""
import hashlib
import json
import logging
import time
import uuid

import pandas as pd

from . import riesgo
from .asignador import a_contratos, asignar
from .estrategias import todas

log = logging.getLogger("cascada")
EPS = 1e-9


def _oid(*partes):
    return hashlib.sha1("|".join(map(str, partes)).encode()).hexdigest()[:32]


def _ms(t):
    return int(pd.Timestamp(t).value // 10**6)


class Motor:
    def __init__(self, cfg, db, datos, bolsa, publico, avisar=None, balas_patrimonio=None, balas_plano=None, balas_nocional=None):
        self.cfg = cfg; self.db = db; self.datos = datos; self.bolsa = bolsa; self.pub = publico
        self.avisar = avisar or (lambda nivel, texto: None)
        self.balas_patrimonio = balas_patrimonio or (lambda: 0.0)
        self.balas_plano = balas_plano or (lambda: True)
        # con cfg.tope_con_balas_real el tope total descuenta el nocional real de 30 balas (protocolo de validación, M7);
        # si no, reserva su peso como siempre
        self.balas_nocional = balas_nocional
        self.est = todas()
        self.mercados = {}
        self.precios = {}

    # ------------------------------------------------------------ libro
    def libro(self):
        return {r["id"]: r for r in self.db.filas("SELECT * FROM lotes WHERE cerrado_ts IS NULL AND cuenta='principal'")}

    def _lote(self, i):
        r = self.db.filas("SELECT * FROM lotes WHERE id=?", (i,))
        return r[0] if r else None

    def _pendientes(self):
        """Símbolos con una orden todavía no aplicada del todo: ahí sólo se reduce hasta resolverla."""
        return {r["simbolo"] for r in self.db.filas("SELECT DISTINCT simbolo FROM ordenes WHERE aplicado=0")}

    def _prio(self, est):
        p = self.cfg.prioridad
        return p.index(est) if est in p else len(p)

    def _op(self, t, est, lote, sim, lado, q, precio, comision, motivo, oid=None):
        tam = self.mercados.get(sim, {}).get("tam") or (self._lote(lote) or {}).get("tam_contrato") or 1.0
        self.db.ejec("INSERT INTO operaciones (ts,cuenta,estrategia,lote,simbolo,lado,contratos,precio,nocional,comision,motivo,modo,client_oid,orden_id)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (_ms(t), "principal", est, lote, sim, lado, q, precio, q * tam * precio, comision, motivo, self.bolsa.modo,
                      oid, oid))

    # ------------------------------------------------------------ cambios de un lote (lo que llenó de verdad)
    def _crear(self, t, est, D, q, precio, comision, motivo="entrada", oid=None):
        sim = D["simbolo"]; tam = self.mercados[sim]["tam"]
        stop = precio - D["lado"] * D["stop_dist"] if D.get("stop_dist") else None
        self.db.ejec("INSERT INTO lotes (id,cuenta,estrategia,simbolo,lado,contratos,tam_contrato,precio_entrada,stop,stop_orden,"
                     "reescalable,abierto_ts,meta,pnl,comision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (D["id"], "principal", est, sim, D["lado"], q, tam, precio, stop, None, int(bool(D.get("reescalable"))),
                      _ms(t), json.dumps(dict(frac=D["frac"])), -comision, comision))
        self._op(t, est, D["id"], sim, "buy" if D["lado"] > 0 else "sell", q, precio, comision, motivo, oid)
        self.avisar("op", f"Entrada {est}: {'largo' if D['lado'] > 0 else 'corto'} {sim} {q:g} contratos a {precio:.6g}")

    def _aumentar(self, t, L, q, precio, comision, motivo="reajuste", oid=None):
        L = self._lote(L["id"]); c = abs(L["contratos"])
        px = (c * L["precio_entrada"] + q * precio) / (c + q)
        self.db.ejec("UPDATE lotes SET contratos=?, precio_entrada=?, pnl=COALESCE(pnl,0)-?, comision=COALESCE(comision,0)+? WHERE id=?",
                     (c + q, px, comision, comision, L["id"]))
        self._op(t, L["estrategia"], L["id"], L["simbolo"], "buy" if L["lado"] > 0 else "sell", q, precio, comision, motivo, oid)

    def _reducir(self, t, L, q, precio, comision, motivo, oid=None):
        """Achica el lote q contratos al precio dado. El resultado se acumula (neto de comisiones); en 0 se cierra."""
        L = self._lote(L["id"])
        if L is None or L["cerrado_ts"] is not None:
            return
        c = abs(L["contratos"]); q = min(q, c)
        if q <= EPS:
            return
        pnl = L["lado"] * q * L["tam_contrato"] * (precio - L["precio_entrada"]) - comision
        self._op(t, L["estrategia"], L["id"], L["simbolo"], "sell" if L["lado"] > 0 else "buy", q, precio, comision, motivo, oid)
        if c - q <= EPS:
            if L.get("stop_orden"):
                self._cancelar_stop(L)
            self.db.ejec("UPDATE lotes SET contratos=0, cerrado_ts=?, precio_salida=?, motivo_salida=?, pnl=COALESCE(pnl,0)+?,"
                         " comision=COALESCE(comision,0)+?, stop_orden=NULL WHERE id=?",
                         (_ms(t), precio, motivo, pnl, comision, L["id"]))
            tot = (L["pnl"] or 0) + pnl
            self.avisar("op", f"Salida {L['estrategia']} {L['simbolo']} ({motivo}) a {precio:.6g}: {tot:+.2f} USDT")
        else:
            self.db.ejec("UPDATE lotes SET contratos=?, pnl=COALESCE(pnl,0)+?, comision=COALESCE(comision,0)+? WHERE id=?",
                         (c - q, pnl, comision, L["id"]))

    # ------------------------------------------------------------ órdenes
    def _ejecutar(self, t, sim, reducir, aumentar):
        """Una orden neta para el símbolo. reducir: [(lote, q, motivo)]; aumentar: [(lote|None, est, deseo, q, motivo)],
        todos del mismo lado. Lo que se compensa entre lotes pasa al precio de mercado; el resto va al exchange."""
        reducir = [x for x in reducir if x[1] > EPS]
        aumentar = [x for x in aumentar if x[3] > EPS]
        if not reducir and not aumentar:
            return None
        lado_pos = reducir[0][0]["lado"] if reducir else aumentar[0][2]["lado"]
        if any(L["lado"] != lado_pos for L, _, _ in reducir) or any(x[2]["lado"] != lado_pos for x in aumentar):
            raise ValueError(f"{sim}: una orden no puede mezclar lados (los separa _operar)")
        px = self.precios.get(sim) or (reducir[0][0]["precio_entrada"] if reducir else None)
        plan = dict(t=str(t), sim=sim, px=px,
                    reducir=[dict(id=L["id"], q=q, motivo=m) for L, q, m in reducir],
                    aumentar=[dict(id=L["id"] if L else None, est=e, D=D, q=q, motivo=m) for L, e, D, q, m in aumentar])
        neto = sum(x[3] for x in aumentar) - sum(x[1] for x in reducir)
        if abs(neto) <= EPS:
            self._aplicar(plan, 0.0, None, 0.0, None)
            return None
        reduce = neto < 0
        lado = "buy" if (neto > 0) == (lado_pos > 0) else "sell"
        oid = _oid(sim, t, uuid.uuid4().hex)
        self.db.ejec("INSERT INTO ordenes (client_oid,ts,simbolo,lado,contratos,reduce,estado,plan) VALUES (?,?,?,?,?,?,?,?)",
                     (oid, _ms(t), sim, lado, abs(neto), int(reduce), "enviando", json.dumps(plan)))
        try:
            r = self.bolsa.enviar_orden(sim, lado, abs(neto), reduce, oid, None if reduce else px)
        except Exception as ex:
            log.exception("orden %s %s %g", sim, lado, abs(neto))
            r = self._consultar(sim, oid)
            if r is None or r["estado"] == "abierta":
                self.db.ejec("UPDATE ordenes SET estado='incierta', error=? WHERE client_oid=?", (str(ex)[:300], oid))
                self.db.incidencia("alta", "orden_incierta", f"Orden {sim} {lado} {abs(neto):g} sin confirmar: {ex}"[:300])
                self.avisar("alta", f"Orden {sim} {lado} {abs(neto):g} sin confirmar ({ex}). Se revisa en el próximo ciclo."[:400])
                self.db.set("conciliacion_ok", False)
                return None
        self._cerrar_orden(oid, plan, r)
        if r["llenado"] < abs(neto) - EPS:
            self.db.incidencia("media" if r["llenado"] > 0 else "alta", "orden_parcial",
                               f"{sim}: pedí {abs(neto):g} contratos ({'reduce' if reduce else 'abre'}), llenó {r['llenado']:g} ({r['estado']})")
        return r

    def _cerrar_orden(self, oid, plan, r):
        """Aplica al libro lo que llenó la orden y anota su estado, todo en UNA transacción (si el proceso muere a mitad
        de camino no queda nada escrito y `_recuperar` la vuelve a encontrar con aplicado=0). Si el exchange la sigue
        mostrando abierta, se aplica lo llenado hasta ahora y queda pendiente: lo que llene después se suma en
        `_recuperar`. Nunca se marca aplicada una orden abierta."""
        o = self.db.filas("SELECT llenado_aplicado, precio_aplicado, comision_aplicada FROM ordenes WHERE client_oid=?", (oid,))
        ya = o[0]["llenado_aplicado"] if o else None
        terminal = r["estado"] != "abierta"
        llen = float(r.get("llenado") or 0.0); com = float(r.get("comision") or 0.0); px = r.get("precio")
        with self.db.transaccion():
            if ya is None:                          # primera vez: lo compensado entre lotes + lo llenado
                self._aplicar(plan, llen, px, com, oid, desde=None, final=terminal)
            elif llen > ya + EPS:                   # llenó más desde la última vez: sólo la diferencia
                px0 = o[0]["precio_aplicado"] or px or 0.0
                px_inc = (px * llen - px0 * ya) / (llen - ya) if px else px0
                self._aplicar(plan, llen, px_inc, com - (o[0]["comision_aplicada"] or 0.0), oid, desde=ya, final=terminal)
            elif terminal:                          # nada nuevo: sólo las incidencias de entradas que no llenaron
                self._aplicar(plan, llen, px, 0.0, oid, desde=llen, final=True)
            self.db.ejec("UPDATE ordenes SET estado=?, orden_id=?, llenado=?, precio=?, comision=?, llenado_aplicado=?,"
                         " precio_aplicado=?, comision_aplicada=?, aplicado=? WHERE client_oid=?",
                         (r["estado"], r.get("orden_id"), llen, px, com, llen, px, com, int(terminal), oid))
        if not terminal:
            self.db.incidencia("alta", "orden_abierta", f"Orden {plan['sim']} {oid[:8]} sigue abierta en el exchange "
                               f"(llenó {llen:g}): se sigue en el próximo ciclo; el símbolo sólo reduce")

    def _aplicar(self, plan, llenado, precio, comision, oid, desde=None, final=True):
        """Reparte entre los lotes lo compensado (al precio de mercado) y lo llenado por la orden (a su precio).
        desde=None: primera aplicación (lo compensado + el llenado [0, llenado)). desde=x: sólo el tramo [x, llenado) que
        la orden llenó después, con `precio` y `comision` de ese tramo. final=False: la orden sigue abierta (todavía no
        se avisa de las entradas sin llenar)."""
        t = pd.Timestamp(plan["t"]); px = plan["px"]
        R = sum(x["q"] for x in plan["reducir"]); I = sum(x["q"] for x in plan["aumentar"])
        interno = min(R, I)
        primera = desde is None
        a, b = (0.0 if primera else desde), llenado
        tramo = max(b - a, 0.0)

        def partes(lista):
            disp_int, cur = interno, 0.0
            for x in lista:
                de_int = min(x["q"], disp_int); disp_int -= de_int
                lo = cur; cur += x["q"] - de_int                  # su parte del llenado de la orden: [lo, cur)
                de_ord = max(0.0, min(cur, b) - max(lo, a))
                if not primera:
                    de_int = 0.0                                   # lo compensado ya se aplicó la primera vez
                tot = de_int + de_ord
                if tot <= EPS:
                    yield x, 0.0, None, 0.0
                    continue
                p = (de_int * px + de_ord * (precio or px)) / tot
                yield x, tot, p, (comision * de_ord / tramo) if tramo > EPS else 0.0

        if I >= R:      # orden de apertura (o nada): los que reducen salen completos contra los que aumentan
            red = [(x, x["q"], px, 0.0) for x in plan["reducir"]] if primera else []
            aum = list(partes(plan["aumentar"]))
        else:           # orden reduce-only: los que aumentan entran completos contra los que reducen
            aum = [(x, x["q"], px, 0.0) for x in plan["aumentar"]] if primera else []
            red = list(partes(plan["reducir"]))
        for x, q, p, com in red:
            if q > EPS:
                self._reducir(t, dict(id=x["id"]), q, p, com, x["motivo"], oid)
        for x, q, p, com in aum:
            L = self._lote(x["id"] or x["D"]["id"])
            if q <= EPS:
                if x["id"] is None and final and (L is None or L["cerrado_ts"] is not None):
                    self.db.incidencia("media", "entrada_sin_llenar", f"{x['est']} {plan['sim']}: la orden no llenó, el lote no se abrió")
                continue
            if L is None:
                self._crear(t, x["est"], x["D"], q, p, com, x["motivo"], oid)
            elif L["cerrado_ts"] is None:
                self._aumentar(t, L, q, p, com, x["motivo"], oid)
            else:
                self.db.incidencia("alta", "llenado_tardio", f"{plan['sim']}: la orden {oid[:8]} llenó {q:g} contratos después "
                                   f"de cerrado el lote {L['id']}: la conciliación lo marca; revisar")

    def _consultar(self, sim, oid):
        try:
            return self.bolsa.estado_orden(sim, oid)
        except Exception:
            return None

    def _recuperar(self):
        """Órdenes no aplicadas del todo (aplicado=0, en cualquier estado): se buscan por clientOid y se aplica lo que
        llenó y no está en el libro. Una orden que sigue abierta se intenta cancelar; mientras no se resuelva, el símbolo
        sólo reduce."""
        for o in self.db.filas("SELECT * FROM ordenes WHERE aplicado=0 ORDER BY ts"):
            oid, plan = o["client_oid"], json.loads(o["plan"])
            r = self._consultar(o["simbolo"], oid)
            if r is not None and r["estado"] == "abierta":
                self._cerrar_orden(oid, plan, r)                  # lo llenado hasta ahora (sigue pendiente)
                try:
                    self.bolsa.cancelar_orden(o["simbolo"], oid)
                except Exception:
                    pass
                r2 = self._consultar(o["simbolo"], oid)
                if r2 is None or r2["estado"] == "abierta":
                    n = o["intentos"] + 1
                    self.db.ejec("UPDATE ordenes SET intentos=? WHERE client_oid=?", (n, oid))
                    if n >= 3 and self.db.incidencia("critica", "orden_abierta_trabada",
                                                     f"Orden {o['simbolo']} {oid[:8]} sigue abierta tras {n} intentos de cancelar: revisar a mano"):
                        self.avisar("critica", f"Orden {o['simbolo']} sigue abierta en el exchange tras {n} intentos de cancelarla: revisar a mano")
                    continue
                r = r2
            if r is not None:
                self._cerrar_orden(oid, plan, r)
                self.db.resolver("orden_incierta"); self.db.resolver("orden_abierta")
                self.avisar("info", f"Orden {o['simbolo']} {o['lado']} confirmada: llenó {r['llenado']:g}")
                continue
            n = o["intentos"] + 1
            if n >= 3 and o["llenado_aplicado"] is None:
                self.db.ejec("UPDATE ordenes SET estado='perdida', aplicado=1, intentos=? WHERE client_oid=?", (n, oid))
                self.db.incidencia("critica", "orden_perdida",
                                   f"Orden {o['simbolo']} {o['lado']} {o['contratos']:g} sin rastro en el exchange: revisar a mano")
                self.avisar("critica", f"Orden {o['simbolo']} {o['lado']} {o['contratos']:g} sin rastro en el exchange: revisar a mano")
            else:
                self.db.ejec("UPDATE ordenes SET intentos=? WHERE client_oid=?", (n, oid))
                if n >= 3:
                    self.db.incidencia("critica", "orden_sin_respuesta", f"Orden {o['simbolo']} {oid[:8]} con llenado parcial "
                                       "aplicado y el exchange no responde por ella: revisar a mano")

    # ------------------------------------------------------------ stops
    def _cancelar_stop(self, L):
        try:
            ok = self.bolsa.cancelar_stop(L["simbolo"], L["stop_orden"])
        except Exception:
            ok = False
        if not ok:
            self.db.incidencia("alta", "stop_sin_cancelar", f"No pude cancelar el stop {L['stop_orden']} de {L['id']} ({L['simbolo']})")
        return ok

    def _revisar_stops(self, t):
        for L in self.libro().values():
            if not L.get("stop_orden"):
                continue
            try:
                e = self.bolsa.estado_stop(L["simbolo"], L["stop_orden"])
            except Exception:
                e = dict(estado="desconocida", llenado=0.0, precio=None)
            if e["estado"] == "abierta":
                continue
            if e["estado"] == "ejecutada":
                q = e.get("llenado") or abs(L["contratos"])
                px = e.get("precio") or L["stop"]
                com = q * L["tam_contrato"] * px * self.cfg.comision
                self.db.ejec("UPDATE lotes SET stop_orden=NULL WHERE id=?", (L["id"],))
                self._reducir(t, L, q, px, com, "stop", L["stop_orden"])
                self.avisar("info", f"Stop ejecutado: {L['estrategia']} {L['simbolo']} {q:g} contratos a {px:.6g}")
            elif e["estado"] == "cancelada" or self._cancelar_stop(L):
                self.db.ejec("UPDATE lotes SET stop_orden=NULL WHERE id=?", (L["id"],))
                self.db.incidencia("media", "stop_repuesto", f"El stop de {L['id']} ({L['simbolo']}) no estaba en el exchange: se repone")

    def _poner_stops(self, t, deseos):
        for L in self.libro().values():
            D = deseos.get(L["id"]) or {}
            nivel = D.get("stop") or L["stop"]
            if not nivel:
                continue
            c = abs(L["contratos"])
            if (L.get("stop_orden") and abs((L.get("stop_contratos") or 0) - c) <= EPS
                    and abs(nivel - L["stop"]) <= 1e-9 * nivel):
                continue
            sim = L["simbolo"]; px = self.precios.get(sim)
            if px and (px - nivel) * L["lado"] <= 0:          # el precio ya pasó el stop: se sale a mercado
                self._ejecutar(t, sim, [(L, c, "stop")], [])
                continue
            if L.get("stop_orden") and not self._cancelar_stop(L):
                continue
            oid, err = None, None
            for _ in range(2):
                try:
                    oid = self.bolsa.enviar_stop(sim, "sell" if L["lado"] > 0 else "buy", c, nivel, _oid(L["id"], "stop", nivel, time.time()))
                    break
                except Exception as ex:
                    err = ex
            if oid:
                self.db.ejec("UPDATE lotes SET stop=?, stop_orden=?, stop_contratos=? WHERE id=?", (nivel, oid, c, L["id"]))
                continue
            self.db.ejec("UPDATE lotes SET stop_orden=NULL WHERE id=?", (L["id"],))
            self.db.incidencia("alta", "sin_stop", f"No pude poner el stop de {L['id']} ({sim}): {err}. Cierro el lote."[:300])
            self.avisar("alta", f"No pude poner el stop de {L['estrategia']} {sim}: cierro el lote. ({err})"[:400])
            self._ejecutar(t, sim, [(L, c, "sin_stop")], [])

    # ------------------------------------------------------------ conciliación
    def _neto_libro(self):
        out = {}
        for L in self.libro().values():
            out[L["simbolo"]] = out.get(L["simbolo"], 0.0) + L["lado"] * abs(L["contratos"])
        return out

    def _conciliar(self, t, curar=False):
        """Libro contra exchange. Con curar=True, si el exchange tiene MENOS que el libro del mismo lado (stop o
        liquidación que no se pudo leer), se achica el libro desde la última prioridad. Si tiene más o del otro lado,
        queda desconciliado: sólo reducciones hasta corregirlo."""
        try:
            real = self.bolsa.posiciones()
        except Exception as ex:
            self.db.incidencia("alta", "conciliacion", f"No pude leer las posiciones: {ex}"[:300])
            self.db.set("conciliacion_ok", False)
            return False
        esperado = self._neto_libro()
        pendientes = self.db.filas("SELECT 1 FROM ordenes WHERE aplicado=0 LIMIT 1")
        dif = []
        for s in sorted(set(esperado) | set(real)):
            e, r = esperado.get(s, 0.0), real.get(s, 0.0)
            tol = max(1e-9, 0.5 * (self.mercados.get(s, {}).get("minimo") or 0) * 0.999)
            if abs(e - r) <= tol:
                continue
            if curar and not pendientes and e and (r == 0 or (r > 0) == (e > 0)) and abs(r) < abs(e):
                falta = abs(e) - abs(r)
                lotes = sorted([L for L in self.libro().values() if L["simbolo"] == s], key=lambda L: -self._prio(L["estrategia"]))
                px = self.precios.get(s) or lotes[0]["precio_entrada"]
                for L in lotes:
                    q = min(falta, abs(L["contratos"]))
                    if q > EPS:
                        self._reducir(t, L, q, px, 0.0, "conciliacion")
                        falta -= q
                msg = f"{s}: el exchange tenía {r:g} y el libro {e:g}; ajusté el libro al exchange"
                self.db.incidencia("alta", "conciliacion_ajuste", msg)
                self.avisar("alta", msg)
                continue
            dif.append(f"{s}: libro {e:g} / exchange {r:g}")
        if dif:
            msg = "Diferencias entre libro y exchange (sólo se reduce hasta corregir): " + "; ".join(dif)
            if self.db.incidencia("alta", "conciliacion", msg[:400]):
                self.avisar("alta", msg[:400])
            self.db.set("conciliacion_ok", False)
            return False
        self.db.resolver("conciliacion")
        self.db.set("conciliacion_ok", True)
        return True

    # ------------------------------------------------------------ corte y cierre total
    def iniciar_corte(self, caida):
        if self.db.get("bloqueado") and self.db.get("liquidando"):
            return
        self.db.set("bloqueado", True)
        self.db.set("liquidando", True)
        self.db.incidencia("critica", "corte_caida", f"Caída {caida:.1%} ≥ corte {self.cfg.corte_caida:.0%}: se cierra todo")
        self.avisar("critica", f"CORTE POR CAÍDA: {caida:.1%}. Cierro todo y bloqueo entradas. Reactivar con /reanudar.")

    def liquidar(self, t, motivo="corte"):
        """Cierra todos los lotes y cualquier resto en el exchange. Devuelve True si la cuenta quedó plana.
        Mientras el estado sea «liquidando» se vuelve a intentar en cada ciclo y en cada control."""
        if not self.mercados:
            self.mercados = self.pub.mercados()
        lib = self.libro()
        sims = sorted({L["simbolo"] for L in lib.values()})
        if sims and not all(s in self.precios for s in sims):
            try:
                self.precios.update(self.pub.precios(sims))
            except Exception:
                pass
        for s in sims:
            try:
                self._ejecutar(t, s, [(L, abs(L["contratos"]), motivo) for L in lib.values() if L["simbolo"] == s], [])
            except Exception as ex:
                log.exception("liquidar %s", s)
                self.db.incidencia("critica", "liquidar", f"No pude cerrar {s}: {ex}"[:300])
        try:
            resto = self.bolsa.posiciones()
            quedan = {L["simbolo"] for L in self.libro().values()}
            for s, c in resto.items():
                if s not in quedan:
                    self.bolsa.enviar_orden(s, "sell" if c > 0 else "buy", abs(c), True, uuid.uuid4().hex[:32])
                    self.db.incidencia("alta", "liquidar_ajeno", f"Cerré {c:g} contratos de {s} que no estaban en el libro")
            plano = not self.bolsa.posiciones() and not self.libro()
        except Exception as ex:
            log.exception("liquidar: posiciones")
            self.db.incidencia("critica", "liquidar", f"No pude comprobar las posiciones: {ex}"[:300])
            plano = False
        if plano and self.db.get("liquidando") and self.balas_plano():
            self.db.set("liquidando", False)
            self.db.resolver("liquidar")
            self.avisar("alta", "Cierre total terminado: todo plano." + (" Entradas bloqueadas hasta /reanudar." if self.db.get("bloqueado") else ""))
        elif not plano and self.db.incidencia("critica", "liquidar_pendiente", "Quedan posiciones abiertas tras el cierre total: reintento"):
            self.avisar("critica", "Quedan posiciones abiertas tras el cierre total: reintento en el próximo control.")
        return plano

    def cerrar_todo(self, t, motivo="manual"):
        return self.liquidar(t, motivo)

    # ------------------------------------------------------------ ciclo
    def _deseos(self, t, lib, universo):
        cfg = self.cfg
        deseos = {}
        abiertos_por_est = {}
        for L in lib.values():
            abiertos_por_est.setdefault(L["estrategia"], {})[L["id"]] = L

        def mantener(ab):
            return [dict(id=i, simbolo=L["simbolo"], lado=L["lado"], frac=json.loads(L["meta"] or "{}").get("frac", 0),
                         reescalable=bool(L["reescalable"]), stop=L["stop"]) for i, L in ab.items()]

        for nombre, e in self.est.items():
            ab = abiertos_por_est.get(nombre, {})
            if not e.decide_en(t) or cfg.pesos.get(nombre, 0) <= 0 and not ab:
                deseos[nombre] = mantener(ab) if cfg.pesos.get(nombre, 0) > 0 else []
                continue
            st = self.db.get(f"est_{nombre}", {})
            # lotes cerrados recientes con el motivo: «asignacion» = el reparto le dio 0 (la estrategia sigue adentro);
            # cualquier otro (stop, tope, corte, manual…) lo cerró un mecanismo externo
            st["_cerrados"] = {r["id"]: r["motivo_salida"] for r in self.db.filas(
                "SELECT id, motivo_salida FROM lotes WHERE estrategia=? AND cerrado_ts IS NOT NULL AND cerrado_ts > ?",
                (nombre, _ms(t - pd.Timedelta(days=120))))}
            try:
                deseos[nombre] = e.paso(t, self.datos, st, ab, universo, self.mercados)
                st.pop("_cerrados", None)
            except Exception as ex:   # una estrategia con error no frena a las demás: mantiene lo que tiene
                log.exception("estrategia %s", nombre)
                self.db.incidencia("alta", f"error_{nombre}", f"{nombre}: {ex}"[:300])
                deseos[nombre] = mantener(ab)
                continue
            self.db.set(f"est_{nombre}", st)
        # nunca reabrir un lote ya cerrado (el id identifica una entrada)
        cerr = {r["id"] for r in self.db.filas("SELECT id FROM lotes WHERE cerrado_ts IS NOT NULL AND cerrado_ts > ?",
                                                (_ms(t - pd.Timedelta(days=120)),))}
        return {n: [L for L in ls if L["id"] not in cerr] for n, ls in deseos.items()}

    def _objetivos(self, deseos, objetivos, lib, precios):
        """Contratos objetivo de cada lote (deseado o abierto). Devuelve id -> dict(est, D, L, c, sim, lado, motivo)."""
        cfg = self.cfg
        out, bajo_minimo = {}, []
        for est, ls in deseos.items():
            for D in ls:
                sim = D["simbolo"]; i = D["id"]; ab = lib.get(i)
                if sim not in self.mercados or sim not in precios:
                    if ab:
                        out[i] = dict(est=est, D=D, L=ab, c=abs(ab["contratos"]), sim=sim, lado=ab["lado"], motivo="")
                    continue
                m = self.mercados[sim]; px = precios[sim]
                if ab is None:
                    c = a_contratos(objetivos.get(i), px, m)
                    if c <= 0:
                        if objetivos.get(i):
                            bajo_minimo.append(f"{est}:{sim}")
                        continue
                    out[i] = dict(est=est, D=D, L=None, c=c, sim=sim, lado=D["lado"], motivo="entrada")
                    continue
                actual = abs(ab["contratos"]); c = actual
                if D.get("reescalable") and objetivos.get(i) is not None:
                    n = a_contratos(objetivos[i], px, m)
                    dif = abs(n - actual) * m["tam"] * px
                    if n <= 0 or dif > max(cfg.reajuste_fraccion * actual * m["tam"] * px, cfg.reajuste_usdt):
                        c = n
                out[i] = dict(est=est, D=D, L=ab, c=c, sim=sim, lado=ab["lado"], motivo="reajuste" if c else "asignacion")
        for i, L in lib.items():
            if i not in out:
                out[i] = dict(est=L["estrategia"], D=None, L=L, c=0.0, sim=L["simbolo"], lado=L["lado"], motivo="salida")
        if bajo_minimo:
            self.db.incidencia("media", "bajo_minimo", "Lotes bajo el contrato mínimo, no abiertos: " + ", ".join(sorted(set(bajo_minimo))))
        else:
            self.db.resolver("bajo_minimo")
        return out

    def _reserva_balas(self, E):
        if getattr(self.cfg, "tope_con_balas_real", False) and self.balas_nocional:
            return max(self.balas_nocional(), 0.0)
        return self.cfg.pesos.get("balas5", 0.0) * E

    def _tope(self, obj, precios, E):
        """Si los objetivos superan 1,05 × tope × patrimonio (por movimiento de precios), recorta desde la última prioridad."""
        cfg = self.cfg
        limite = 1.05 * (cfg.tope_nocional * E - self._reserva_balas(E))

        def valor(o):
            return o["c"] * self.mercados[o["sim"]]["tam"] * precios.get(o["sim"], 0)
        n = sum(valor(o) for o in obj.values() if o["sim"] in self.mercados)
        if n <= limite:
            return
        exceso = n - limite
        for est in reversed([p for p in cfg.prioridad if p != "balas5"]):
            for o in [x for x in obj.values() if x["est"] == est and x["c"] > 0]:
                if exceso <= 0:
                    break
                v = valor(o)
                if v <= exceso:
                    o["c"] = 0.0; exceso -= v
                else:
                    m = self.mercados[o["sim"]]; px = precios[o["sim"]]
                    quitar = int(exceso / (m["tam"] * px) / m["minimo"] + 1) * m["minimo"]
                    o["c"] = max(o["c"] - quitar, 0.0); exceso = 0
                o["motivo"] = "tope"
        self.db.incidencia("media", "tope", f"Nocional {n:.0f} > límite {limite:.0f}: recorté desde la última prioridad")

    def _operar(self, t, obj, solo_reducir):
        pendientes = self._pendientes()
        por_sim = {}
        for i, o in obj.items():
            por_sim.setdefault(o["sim"], []).append(o)
        planes = []
        for sim, os_ in por_sim.items():
            solo_red = solo_reducir or sim in pendientes      # con una orden sin resolver en el símbolo, sólo se reduce
            red, aum, despues, omitidos = [], [], [], []
            for o in os_:
                if o["L"]:
                    d = o["c"] - abs(o["L"]["contratos"])
                    if d < -EPS:
                        red.append((o["L"], -d, o["motivo"] or "reajuste"))
                    elif d > EPS and not solo_red:
                        aum.append((o["L"], o["est"], o["D"], d, "reajuste"))
            nuevos = sorted([o for o in os_ if not o["L"] and o["c"] > EPS], key=lambda o: self._prio(o["est"]))
            if nuevos and solo_red and not solo_reducir:
                self.db.incidencia("media", "orden_pendiente", f"{sim}: hay una orden sin resolver; no se abre nada nuevo hasta resolverla")
            if nuevos and not solo_red:
                abiertos = [o for o in os_ if o["L"]]
                quedan = [o for o in abiertos if o["c"] > EPS]
                # un solo lado por símbolo: el de los lotes que siguen abiertos; si no queda ninguno (símbolo plano o que
                # se cierra ahora), el de la entrada de mayor prioridad, aunque haya pedidos de los dos lados
                lado = quedan[0]["L"]["lado"] if quedan else nuevos[0]["lado"]
                lado_libro = abiertos[0]["L"]["lado"] if abiertos else lado
                for o in nuevos:
                    x = (None, o["est"], o["D"], o["c"], "entrada")
                    if o["lado"] != lado:
                        omitidos.append(o["est"])
                    elif lado == lado_libro:
                        aum.append(x)
                    else:                   # del otro lado de lotes que se cierran en este ciclo: después de cerrarlos
                        despues.append(x)
                if omitidos:
                    self.db.incidencia("baja", "lado_opuesto", f"{sim}: " + ", ".join(sorted(set(omitidos)))
                                       + " quiso abrir del lado contrario; un símbolo va de un solo lado (manda la prioridad)")
            red.sort(key=lambda x: (abs(x[0]["contratos"]) - x[1] > EPS, -self._prio(x[0]["estrategia"])))
            aum.sort(key=lambda x: self._prio(x[1]))
            neto = sum(x[3] for x in aum) - sum(x[1] for x in red)
            planes.append((neto > 0, sim, red, aum, despues))
        for _, sim, red, aum, despues in sorted(planes, key=lambda p: p[0]):     # primero lo que libera margen
            try:
                self._ejecutar(t, sim, red, aum)
                if despues and not any(L["simbolo"] == sim for L in self.libro().values()) and sim not in self._pendientes():
                    self._ejecutar(t, sim, [], despues)
            except Exception as ex:
                log.exception("operar %s", sim)
                self.db.incidencia("alta", "operar", f"{sim}: {ex}"[:300])

    def ciclo(self, t, precios=None, velas_cerradas=None):
        """t: momento de decisión (cierre de vela). precios: base -> último precio. velas_cerradas: base -> (o,h,l)."""
        t = pd.Timestamp(t)
        cfg, db = self.cfg, self.db
        self.mercados = self.pub.mercados()
        universo = [s for s in self.datos.universo(t) if s in self.mercados]
        lib = self.libro()
        bases = sorted(set(universo) | {"BTC", "ETH"} | {L["simbolo"] for L in lib.values()})
        if precios is None:
            precios = self.pub.precios([b for b in bases if b in self.mercados])
        self.precios = dict(precios)
        if self.bolsa.modo == "papel":
            self.bolsa.fijar_precios(precios)
            if velas_cerradas:
                self.bolsa.revisar_stops(velas_cerradas)

        # 1–3) pendientes, stops y conciliación antes de decidir
        self._recuperar()
        self._revisar_stops(t)
        conciliado = self._conciliar(t, curar=True)
        lib = self.libro()

        # 4) patrimonio y riesgo
        E_main = self.bolsa.patrimonio()
        E_bal = self.balas_patrimonio()
        E = E_main + E_bal
        er = riesgo.actualizar_patrimonio(db, t, E, E_main, E_bal, lib, precios, cfg)
        if er["corte"] and not db.get("bloqueado"):
            self.iniciar_corte(er["caida"])
        if db.get("liquidando"):
            plano = self.liquidar(t, "corte")
            db.set("ultimo_ciclo", dict(t=str(t), ts=time.time(), E=E, E_main=E_main, E_bal=E_bal))
            return dict(t=str(t), corte=True, plano=plano)
        if er["alerta"]:
            if db.incidencia("alta", "alerta_caida", f"Caída desde el máximo {er['caida']:.1%} ≥ alerta {cfg.alerta_caida:.0%}"):
                self.avisar("alta", f"Alerta: caída desde el máximo {er['caida']:.1%}")
        else:
            db.resolver("alerta_caida")

        # 5) estrategias, asignación, objetivos y tope
        deseos = self._deseos(t, lib, universo)
        w_bal = cfg.pesos.get("balas5", 0.0)
        presupuesto = cfg.tope_nocional * E - self._reserva_balas(E)
        prio = [p for p in cfg.prioridad if p != "balas5"]
        objetivos, resumen = asignar(E, presupuesto, cfg.pesos, prio, deseos, lib, precios, self.mercados)
        for est, r in resumen.items():
            db.ejec("INSERT OR REPLACE INTO asignacion VALUES (?,?,?,?,?)", (_ms(t), est, r["pedido"], r["concedido"], r["g"]))
        obj = self._objetivos(deseos, objetivos, lib, precios)
        self._tope(obj, precios, E)

        # 6–7) órdenes (en pausa, bloqueo o desconciliación sólo se reduce)
        solo_reducir = bool(db.get("bloqueado") or db.get("pausado") or not conciliado)
        self._operar(t, obj, solo_reducir)

        # 8–9) stops y conciliación final
        self._poner_stops(t, {D["id"]: D for ls in deseos.values() for D in ls})
        self._conciliar(t)
        db.set("ultimo_ciclo", dict(t=str(t), ts=time.time(), E=E, E_main=E_main, E_bal=E_bal))
        return dict(t=str(t), E=E, resumen=resumen)
