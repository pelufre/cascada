"""Motor de 30 balas 10x con dos cargadores en XBTUSDM (futuro inverso, margen aislado).

Capital (BTC) de cada campaña, fijado al abrirla: Wc = BTC en la cuenta de futuros + BTC declarado afuera.
  cargador 1 = 40 % Wc (30 balas a posición) · reserva = 40 % Wc (mitad al abrir, resto al 12 % de la liquidación) ·
  cargador 2 = 20 % Wc (30 balas, sólo margen; puede estar afuera del exchange: el bot pide el depósito).

Qué hace solo y qué pregunta:
  · Solo (sin confirmación): todos los aportes de MARGEN (reserva inicial y resto, balas sólo-margen, cargador 2,
    emergencia cerca de la liquidación).
  · Con confirmación (/si ID): abrir campaña, comprar contratos de una recarga y cerrar.

Estado (clave "balas10" en la base):
  activo, inicio, Wc, c1, r, c2, c1_usado, c2_usado, c2_debe, ini_puesta, resd, rec, afuera, campanas, ...
"""
import json
import logging
import time
import uuid

import pandas as pd

from . import senales as S

log = logging.getLogger("balas10.motor")
POLVO = 2e-6          # BTC por debajo de esto se ignoran


def ahora():
    return pd.Timestamp.now("UTC").tz_localize(None)


def ms(t):
    return int(pd.Timestamp(t).value // 10**6)


def btc(x, d=4):
    return "—" if x is None else f"{x:.{d}f}".replace(".", ",")


def pct(x, d=1):
    return "—" if x is None or x != x else f"{x * 100:+.{d}f} %".replace(".", ",")


def usd(x):
    return "—" if x is None else f"{x:,.0f}".replace(",", ".")


ESTADO0 = dict(activo=False, inicio=None, Wc=0.0, c1=0.0, r=0.0, c2=0.0, c1_usado=0.0, c2_usado=0.0, c2_debe=0.0,
               ini_puesta=False, ini_aportado=0.0, resd=False, rec=False, afuera=0.0, campanas=0, liquidaciones=0, ultima_senal=None,
               resultado_campanas=[])


class Motor:
    def __init__(self, cfg, db, bolsa, fuente, avisar=None, p=None):
        self.cfg, self.db, self.bolsa, self.fuente = cfg, db, bolsa, fuente
        self.p = dict(S.P, **(p or {}))
        self.avisar = avisar or (lambda n, t: log.info("[%s] %s", n, t))

    # ============================================================================================ estado
    @property
    def st(self):
        s = self.db.get("balas10")
        return dict(ESTADO0, **(s or {}))

    def guardar(self, s):
        self.db.set("balas10", s)

    def bala1(self, s):
        return s["c1"] / self.p["B1"] if s["c1"] else 0.0

    def bala2(self, s):
        return s["c2"] / self.p["B2"] if s["c2"] else 0.0

    def usadas(self, s):
        u1 = int(round(s["c1_usado"] / self.bala1(s))) if s["c1"] else 0
        u2 = int(round((s["c2_usado"] + s["c2_debe"]) / self.bala2(s))) if s["c2"] else 0
        return min(u1, self.p["B1"]), min(u2, self.p["B2"])

    def reserva_pendiente(self, s):
        """BTC de la reserva que todavía no entró como margen (tiene que quedar disponible en la cuenta)."""
        if not s["activo"]:
            return 0.0
        q = 0.0
        if not s["ini_puesta"]:
            q += max(s["r"] * self.p["pref"] - s["ini_aportado"], 0.0)
        if not s["resd"]:
            q += s["r"] * (1 - self.p["pref"])
        return q

    def c1_restante(self, s):
        return max(s["c1"] - s["c1_usado"], 0.0) if s["activo"] else 0.0

    def libre_para_c2(self, s, libre):
        return max(libre - self.reserva_pendiente(s) - self.c1_restante(s), 0.0)

    # ============================================================================================ registro
    def _op(self, lado, contratos, precio, motivo, orden=None, margen=None):
        self.db.ejec("INSERT INTO operaciones (ts,parte,simbolo,lado,cantidad,precio,usdt,comision,motivo,modo,orden)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (int(time.time() * 1000), "balas10", "XBTUSDM", lado, contratos, precio,
                      contratos if lado != "margen" else (margen or 0) * (precio or 0), 0.0, motivo, self.bolsa.modo, orden))

    def _margen(self, s, monto, motivo, informe):
        """Agrega margen (automático). Devuelve lo agregado."""
        if monto < POLVO:
            return 0.0
        hecho = self.bolsa.agregar_margen(monto)
        if hecho > 0:
            self._op("margen", 0, self.precio_seguro(), motivo, margen=hecho)
            informe.append(f"margen +{btc(hecho, 5)} BTC ({motivo})")
        return hecho

    def precio_seguro(self):
        try:
            return self.bolsa.precio()
        except Exception:
            return None

    # ============================================================================================ señales
    def senales(self, t_cierre):
        t_cierre = pd.Timestamp(t_cierre)
        c4 = self.fuente.btc_4h()
        dC = self.fuente.btc_1d()
        c4 = c4[c4.index + pd.Timedelta(hours=4) <= t_cierre]
        dC = dC[dC.index + pd.Timedelta(days=1) <= t_cierre]
        if c4.index[-1] != t_cierre - pd.Timedelta(hours=4):
            raise RuntimeError(f"Falta la vela de 4 h que cierra {t_cierre} (última {c4.index[-1]})")
        if dC.index[-1] != t_cierre.normalize() - pd.Timedelta(days=1):
            raise RuntimeError(f"Falta el cierre diario del {(t_cierre.normalize() - pd.Timedelta(days=1)).date()}")
        return S.senal(c4, dC, t_cierre, self.p)

    # ============================================================================================ conciliación
    def conciliar(self, s, pos):
        """Compara el estado con la cuenta. Devuelve False si no se puede operar."""
        if s["activo"] and pos is None:
            s.update(activo=False)
            s["liquidaciones"] += 1
            self.guardar(s)
            self.db.incidencia("critica", "posicion", "La posición XBTUSDM ya no está (liquidación o cierre manual)")
            self.avisar("critica", "30 balas: la posición XBTUSDM ya no está en KuCoin (¿liquidación o cierre manual?). "
                                   "Campaña terminada; revisá la cuenta.")
            return False
        if not s["activo"] and pos is not None:
            self.db.incidencia("alta", "posicion", "Hay una posición XBTUSDM sin campaña registrada: usar `adoptar`")
            self.avisar("alta", "30 balas: hay una posición XBTUSDM que el bot no conoce. No opero hasta que la adoptes "
                                "(python -m balas10.cli adoptar …).")
            return False
        if pos is not None and str(pos.get("modo", "ISOLATED")).upper() != "ISOLATED":
            self.db.incidencia("critica", "posicion", "La posición XBTUSDM no está en margen aislado")
            self.avisar("critica", "30 balas: la posición XBTUSDM no está en margen AISLADO. No opero.")
            return False
        if pos is not None and pos.get("auto_margen"):
            try:
                self.bolsa.auto_margen(False)
                self.avisar("media", "30 balas: apagué el depósito automático de margen de KuCoin (lo maneja el bot).")
            except Exception as e:
                self.avisar("alta", f"30 balas: el depósito automático de margen de KuCoin está activo y no pude apagarlo: {e}")
        return True

    # ============================================================================================ ciclo de 4 h
    def ciclo(self, t_cierre):
        """Calcula la señal del cierre, ejecuta lo automático y devuelve (informe, decisión que requiere OK | None)."""
        s = self.st
        sig = self.senales(t_cierre)
        s["ultima_senal"] = sig
        self.guardar(s)
        pos = self.bolsa.posicion()
        informe = []
        if not self.conciliar(s, pos):
            return informe, None
        s = self.st
        dec = None
        if s["activo"]:
            rec0 = s["rec"]
            salir = (rec0 and sig["sobre_previa"] and not sig["sobre"]) or (sig["dia"] and not sig["regimen"])
            s["rec"] = rec0 or sig["sobre"]
            self.guardar(s)
            if salir:
                motivo = "régimen semanal apagado" if (sig["dia"] and not sig["regimen"]) else f"cierre bajo la SMA{self.p['ma']}"
                dec = dict(tipo="salida+entrada" if sig["entrada"] else "salida", motivo=motivo, levm=sig["levm"],
                           rec=sig["sobre"], vela=sig["t"])
            elif sig["dia"]:
                dec = self.recarga(s, pos, sig, informe)
        elif sig["entrada"]:
            dec = dict(tipo="entrada", motivo="régimen activo, RSI(2) ≤ 15 y momento 90 d positivo", levm=sig["levm"],
                       rec=sig["sobre"], vela=sig["t"])
        self.control(informe)
        return informe, dec

    def recarga(self, s, pos, sig, informe):
        """Recarga diaria. El margen sale solo; la compra de contratos vuelve como decisión a confirmar."""
        roe = pos["pnl"] / pos["margen"] if pos and pos["margen"] > 0 else 0.0
        add, ratio = S.balas_por_roe(roe)
        u1, u2 = self.usadas(s)
        B1, B2 = self.p["B1"], self.p["B2"]
        a1 = min(add, B1 - u1)
        a2 = 0
        if u1 + a1 >= B1:
            a2 = min(add - a1 if a1 > 0 else add, B2 - u2)
        if a1 + a2 <= 0:
            return None
        b1, b2 = self.bala1(s), self.bala2(s)
        m1 = b1 * a1 * (1 - ratio)               # parte sólo-margen del cargador 1
        p1 = b1 * a1 * ratio                     # parte a posición (requiere OK)
        m2 = b2 * a2                             # cargador 2: todo a margen
        texto = f"ROE {pct(roe)}: {add} balas" + (f" (cargador 1: {a1}, {ratio:.0%} a posición)" if a1 else "") + \
                (f" (cargador 2: {a2}, sólo margen)" if a2 else "")
        informe.append("Recarga diaria · " + texto)
        if m1 > POLVO:
            cuenta = self.bolsa.cuenta()
            hecho = self._margen(s, min(m1, max(cuenta["libre"] - self.reserva_pendiente(s), 0)), f"{a1} balas sólo margen", informe)
            s["c1_usado"] += hecho
            if hecho < m1 - POLVO:
                informe.append(f"faltó saldo para {btc(m1 - hecho, 5)} BTC de margen del cargador 1")
        if m2 > POLVO:
            cuenta = self.bolsa.cuenta()
            disp = self.libre_para_c2(s, cuenta["libre"])
            hecho = self._margen(s, min(m2, disp), f"{a2} balas del cargador 2", informe)
            s["c2_usado"] += hecho
            falta = m2 - hecho
            if falta > POLVO:
                s["c2_debe"] += falta
                self.avisar("alta", f"30 balas: el cargador 2 tiene que aportar {btc(falta, 5)} BTC de margen y no están en la "
                                    f"cuenta de futuros (deuda acumulada {btc(s['c2_debe'], 5)} BTC). Depositá BTC en FUTUROS: "
                                    f"el bot lo agrega solo en el próximo control (15 min). Afuera declarados: {btc(s['afuera'], 4)} BTC.")
        self.guardar(s)
        if p1 * sig["levm"] * self.p["lev"] * sig["cierre"] >= 1:
            return dict(tipo="recarga", motivo=texto, balas=a1, ratio=ratio, levm=sig["levm"], vela=sig["t"])
        if a1 and p1 > 0:
            s["c1_usado"] += p1
            self.guardar(s)
        return None

    # ============================================================================================ control (cada 15 min)
    def control(self, informe=None):
        informe = informe if informe is not None else []
        s = self.st
        if not s["activo"]:
            return informe
        pos = self.bolsa.posicion()
        if pos is None:
            self.conciliar(s, pos)
            return informe
        cuenta = self.bolsa.cuenta()
        dist = pos["marca"] / pos["liq"] - 1 if pos["liq"] else None
        if not s["ini_puesta"]:
            q = s["r"] * self.p["pref"] - s["ini_aportado"]
            hecho = self._margen(s, min(q, cuenta["libre"]), "reserva inicial", informe)
            s["ini_aportado"] += hecho
            if hecho >= q - POLVO:
                s["ini_puesta"] = True
            else:
                self.avisar("alta", f"30 balas: faltan {btc(q - hecho, 5)} BTC de la reserva inicial en la cuenta de futuros.")
            self.guardar(s)
            pos = self.bolsa.posicion(); cuenta = self.bolsa.cuenta()
            dist = pos["marca"] / pos["liq"] - 1 if pos and pos["liq"] else dist
        if not s["resd"] and dist is not None and dist <= self.p["rt"]:
            q = s["r"] * (1 - self.p["pref"])
            hecho = self._margen(s, min(q, cuenta["libre"]), f"resto de la reserva (a {pct(dist)} de la liquidación)", informe)
            s["resd"] = True
            self.guardar(s)
            self.avisar("alta", f"30 balas: el precio quedó a {pct(dist)} de la liquidación; entró el resto de la reserva "
                                f"({btc(hecho, 5)} BTC).")
            if hecho < q - POLVO:
                self.avisar("critica", f"30 balas: faltaron {btc(q - hecho, 5)} BTC de la reserva en la cuenta de futuros.")
            pos = self.bolsa.posicion(); cuenta = self.bolsa.cuenta()
            dist = pos["marca"] / pos["liq"] - 1 if pos and pos["liq"] else dist
        if s["c2_debe"] > POLVO:
            disp = self.libre_para_c2(s, cuenta["libre"])
            hecho = self._margen(s, min(s["c2_debe"], disp), "cargador 2 (depósito pendiente)", informe)
            if hecho > 0:
                s["c2_debe"] -= hecho; s["c2_usado"] += hecho
                s["afuera"] = max(s["afuera"] - hecho, 0.0)
                self.guardar(s)
                cuenta = self.bolsa.cuenta()
        emerg = self.cfg.emergencia_dist
        if dist is not None and dist <= emerg:
            extra = cuenta["libre"] - 1e-5
            if extra > POLVO and self.cfg.emergencia_usar_libre:
                hecho = self._margen(s, extra, f"emergencia: a {pct(dist)} de la liquidación", informe)
                s["c2_usado"] += hecho
                self.guardar(s)
            if self.db.incidencia("critica", "liquidacion", f"A {pct(dist)} de la liquidación", dedupe_horas=1):
                self.avisar("critica", f"30 balas: el precio está a {pct(dist)} de la liquidación ({usd(pos['liq'])}). "
                                       f"Queda afuera del exchange: {btc(s['afuera'], 4)} BTC. Depositá en FUTUROS y el bot "
                                       f"lo agrega como margen.")
        return informe

    # ============================================================================================ ejecución (con OK)
    def ejecutar(self, dec):
        tipo = dec["tipo"]
        informe = []
        if tipo in ("salida", "salida+entrada"):
            self._salida(dec, informe)
            if tipo == "salida+entrada":
                self._entrada(dec, informe)
        elif tipo == "entrada":
            self._entrada(dec, informe)
        elif tipo == "recarga":
            self._recarga_posicion(dec, informe)
        self.control(informe)
        return informe

    def _salida(self, dec, informe):
        s = self.st
        if not s["activo"]:
            informe.append("no había campaña abierta")
            return
        f = self.bolsa.cerrar_todo()
        if f:
            self._op("venta", f["contratos"], f["precio"], dec["motivo"], f["orden"])
        time.sleep(0 if self.bolsa.modo == "papel" else 2)
        eq = self.bolsa.cuenta()["equity"] + s["afuera"]
        res = eq / s["Wc"] - 1 if s["Wc"] else None
        informe.append(f"cerrada la campaña ({dec['motivo']}): {int(f['contratos']) if f else 0} contratos a "
                       f"{usd(f['precio']) if f else '—'} · resultado {pct(res)} en BTC")
        s["resultado_campanas"] = (s["resultado_campanas"] + [dict(inicio=s["inicio"], fin=str(ahora()), res=res)])[-200:]
        s.update(activo=False, c1_usado=0.0, c2_usado=0.0, c2_debe=0.0, ini_puesta=False, ini_aportado=0.0, resd=False, rec=False)
        s["campanas"] += 1
        self.guardar(s)

    def _entrada(self, dec, informe):
        s = self.st
        if s["activo"] or self.bolsa.posicion() is not None:
            informe.append("ya hay una posición abierta: no abro otra")
            return
        p = self.p
        cuenta = self.bolsa.cuenta()
        Wc = cuenta["equity"] + s["afuera"]
        c1, r, c2 = p["f1"] * Wc, p["fr"] * Wc, p["f2"] * Wc
        b1 = c1 / p["B1"] * p["init"]
        px = self.bolsa.precio()
        n = int(round(b1 * p["lev"] * dec["levm"] * px))
        hace_falta = b1 + r * p["pref"]
        if cuenta["libre"] < hace_falta * 1.002:
            raise RuntimeError(f"No alcanza el BTC en futuros para abrir: hacen falta {btc(hace_falta, 5)} "
                               f"(bala + mitad de la reserva) y hay {btc(cuenta['libre'], 5)} libres")
        f = self.bolsa.comprar(n, p["lev"])
        s.update(activo=True, inicio=str(ahora()), Wc=Wc, c1=c1, r=r, c2=c2, c1_usado=b1, c2_usado=0.0, c2_debe=0.0,
                 ini_puesta=False, ini_aportado=0.0, resd=p["pref"] >= 1, rec=bool(dec.get("rec")))
        self.guardar(s)
        if f:
            self._op("compra", f["contratos"], f["precio"], "inicio de campaña", f["orden"])
            informe.append(f"nueva campaña: {int(f['contratos'])} contratos a {usd(f['precio'])} "
                           f"({p['lev'] * dec['levm']:.2f}x) · capital {btc(Wc)} BTC "
                           f"(cargador 1 {btc(c1)}, reserva {btc(r)}, cargador 2 {btc(c2)})")
            tomado = f["contratos"] / f["precio"] / p["lev"]
            self._margen(s, b1 - tomado, "completar margen de la bala", informe)

    def _recarga_posicion(self, dec, informe):
        s = self.st
        if not s["activo"]:
            informe.append("no hay campaña abierta")
            return
        p = self.p
        q = self.bala1(s) * dec["balas"] * dec["ratio"]
        q = min(q, self.c1_restante(s))
        cuenta = self.bolsa.cuenta()
        q = min(q, max(cuenta["libre"] - self.reserva_pendiente(s), 0.0))
        px = self.bolsa.precio()
        n = int(q * p["lev"] * dec["levm"] * px)
        if n < 1:
            informe.append("recarga: no queda saldo del cargador 1")
            return
        f = self.bolsa.comprar(n, p["lev"])
        if f:
            self._op("compra", f["contratos"], f["precio"], f"recarga {dec['balas']} balas", f["orden"])
            informe.append(f"recarga: +{int(f['contratos'])} contratos a {usd(f['precio'])}")
            tomado = f["contratos"] / f["precio"] / p["lev"]
            self._margen(s, q - tomado, "completar margen de la recarga", informe)
            s["c1_usado"] += q
            self.guardar(s)

    # ============================================================================================ adopción
    def adoptar(self, c1, r, c2_afuera, balas, desde, rec=None, c2_en_cuenta=0.0, reserva_inicial_puesta=False):
        """Toma una campaña ya abierta en KuCoin. c1/r/c2 en BTC; `balas` usadas del cargador 1."""
        pos = self.bolsa.posicion()
        if pos is None:
            raise RuntimeError("No hay posición XBTUSDM abierta para adoptar")
        if rec is None:
            c4 = self.fuente.btc_4h()
            ma = c4.rolling(self.p["ma"]).mean()
            tramo = c4[c4.index >= pd.Timestamp(desde)]
            rec = bool((tramo > ma.reindex(tramo.index)).any())
        c2 = c2_afuera + c2_en_cuenta
        Wc = c1 + r + c2
        s = dict(ESTADO0, activo=True, inicio=str(pd.Timestamp(desde)), Wc=Wc, c1=c1, r=r, c2=c2,
                 c1_usado=c1 * min(balas, self.p["B1"]) / self.p["B1"], c2_usado=0.0, c2_debe=0.0,
                 ini_puesta=bool(reserva_inicial_puesta), resd=False, rec=rec, afuera=c2_afuera,
                 campanas=self.st["campanas"], liquidaciones=self.st["liquidaciones"])
        self.guardar(s)
        return s, pos

    # ============================================================================================ decisiones
    def nueva_decision(self, t_cierre, dec):
        for p in self.db.filas("SELECT id FROM decisiones WHERE estado='pendiente'"):
            self.marcar(p["id"], "reemplazada")
        did = uuid.uuid4().hex[:6]
        vence = self.vencimiento(t_cierre)
        self.db.ejec("INSERT INTO decisiones (id,ts,vela,tipo,estado,resumen,datos,vence_ts,resultado) VALUES (?,?,?,?,?,?,?,?,?)",
                     (did, int(time.time() * 1000), str(t_cierre), dec["tipo"], "nueva", self.texto_decision(dec),
                      json.dumps(dec), ms(vence), None))
        return did

    def vencimiento(self, t_cierre):
        t = pd.Timestamp(t_cierre)
        h = self.cfg.vence_hora_local
        if h:
            loc = pd.Timestamp.now(tz=self.cfg.zona_horaria)
            hh, mm = (int(x) for x in h.split(":"))
            v = loc.normalize() + pd.Timedelta(hours=hh, minutes=mm)
            if v <= loc:
                v += pd.Timedelta(days=1)
            return v.tz_convert("UTC").tz_localize(None)
        return t + pd.Timedelta(hours=self.cfg.vence_h)

    def marcar(self, did, estado, resultado=None):
        self.db.ejec("UPDATE decisiones SET estado=?, resultado=COALESCE(?, resultado) WHERE id=?", (estado, resultado, did))

    def decision(self, did):
        r = self.db.filas("SELECT * FROM decisiones WHERE id=?", (did,))
        if not r:
            return None
        d = r[0]
        d["datos"] = json.loads(d["datos"])
        return d

    def pendiente(self):
        r = self.db.filas("SELECT * FROM decisiones WHERE estado='pendiente' ORDER BY ts DESC LIMIT 1")
        if not r:
            return None
        d = r[0]
        if d["vence_ts"] and time.time() * 1000 > d["vence_ts"]:
            self.marcar(d["id"], "vencida")
            self.avisar("media", f"30 balas: la decisión #{d['id']} venció sin respuesta; no se ejecutó.")
            return None
        d["datos"] = json.loads(d["datos"])
        return d

    def texto_decision(self, dec):
        t = dec["tipo"]
        if t == "entrada":
            return f"ABRIR campaña ({dec['motivo']}): 1 bala a {self.p['lev'] * dec['levm']:.2f}x + mitad de la reserva como margen"
        if t == "salida":
            return f"CERRAR la campaña ({dec['motivo']})"
        if t == "salida+entrada":
            return f"CERRAR la campaña ({dec['motivo']}) y ABRIR otra en la misma vela (sigue la señal de entrada)"
        if t == "recarga":
            return f"COMPRAR contratos de la recarga diaria: {dec['motivo']}"
        return t

    # ============================================================================================ estado legible
    def valuar(self):
        s = self.st
        pos = self.bolsa.posicion()
        cuenta = self.bolsa.cuenta()
        px = pos["marca"] if pos else self.precio_seguro()
        return dict(s=s, pos=pos, cuenta=cuenta, precio=px, total_btc=cuenta["equity"] + s["afuera"],
                    total_usd=(cuenta["equity"] + s["afuera"]) * px if px else None)

    def texto_estado(self):
        v = self.valuar()
        s, pos, c = v["s"], v["pos"], v["cuenta"]
        lin = [f"Capital {btc(v['total_btc'])} BTC (≈ {usd(v['total_usd'])} USD) · futuros {btc(c['equity'])} · "
               f"libre {btc(c['libre'])} · afuera {btc(s['afuera'])}"]
        if s["activo"] and pos:
            u1, u2 = self.usadas(s)
            dist = pos["marca"] / pos["liq"] - 1 if pos["liq"] else None
            lin += [f"Campaña desde {str(s['inicio'])[:16]} · {int(pos['contratos'])} contratos · entrada {usd(pos['entrada'])} · "
                    f"marca {usd(pos['marca'])}",
                    f"Liquidación {usd(pos['liq'])} ({pct(dist)}) · margen {btc(pos['margen'])} BTC · ROE {pct(pos['pnl'] / pos['margen'] if pos['margen'] else None)}",
                    f"Cargador 1 {u1}/{self.p['B1']} · cargador 2 {u2}/{self.p['B2']}"
                    + (f" (debe {btc(s['c2_debe'], 5)} BTC)" if s["c2_debe"] > POLVO else "")
                    + f" · reserva: inicial {'puesta' if s['ini_puesta'] else 'pendiente'}, resto {'puesto' if s['resd'] else 'en espera'}",
                    f"Cruzó sobre la SMA{self.p['ma']}: {'sí' if s['rec'] else 'no'} (la salida por SMA se activa después)"]
        else:
            lin.append("Sin campaña abierta")
        g = s.get("ultima_senal")
        if g:
            lin.append(f"Última señal {g['t'][:16]} UTC · régimen {'ALCISTA' if g['regimen'] else 'apagado'} · RSI(2) {g['rsi']:.1f} · "
                       f"SMA{self.p['ma']} {usd(g['sma'])} · apalancamiento {self.p['lev'] * g['levm']:.2f}x · "
                       f"momento 90 d {'+' if g['momento'] else '−'}")
        return "\n".join(lin)
