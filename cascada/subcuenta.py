"""Capital de 30 balas en real: reequilibrio entre la cuenta principal y la subcuenta (protocolo E4).

Como en la validación y en el papel: mientras 30 balas no tiene campaña abierta, la subcuenta se iguala a
peso × patrimonio total (principal + subcuenta). Durante una campaña no se toca. Se mueve sólo USDT:
  principal → subcuenta: futuros USDT-M de la principal (CONTRACT) → «main» de la subcuenta → «trade» (donde compra BTC)
  subcuenta → principal: «trade» → «main» de la subcuenta → futuros de la principal

La transferencia entre cuentas la hace la clave de la cuenta PRINCIPAL con el permiso «FlexTransfers» de KuCoin (mueve
fondos sólo entre tus cuentas; no es permiso de retiro; KuCoin exige que la clave esté restringida por IP).

Cada transferencia lógica queda registrada (tabla `transferencias`) con un id durable, sus pasos y el saldo antes de
cada paso. Ante un error de red o una respuesta perdida, el paso NO se da por fallido: se mira el saldo hasta confirmar
que se hizo; si no se puede confirmar, la transferencia queda «incierta», no se prueba otra ruta ni se deshace nada, y
30 balas queda bloqueada hasta resolverla con los saldos en un ciclo siguiente. Sólo un rechazo explícito del exchange
hace probar la otra ruta. Probar primero con `cli transferencia-prueba`.
"""
import json
import logging
import time
import uuid

log = logging.getLogger("cascada.subcuenta")
MINIMO_USDT = 10.0          # diferencias menores no se transfieren
MINIMO_REL = 0.01           # ni las menores al 1 % del objetivo
COLCHON_PRINCIPAL = 0.10    # la principal conserva al menos 10 % de su patrimonio libre como margen
TOL_USDT = 0.011            # tolerancia al comparar saldos para confirmar un paso


class TransferenciaIncierta(RuntimeError):
    """Un paso pudo haberse hecho (sin respuesta) y el saldo no lo confirma: no se reintenta ni se prueba otra ruta."""


class _Rechazada(RuntimeError):
    """El exchange rechazó el paso de forma explícita: no se movió nada."""


def es_error_de_red(e):
    """Sin respuesta o error de conexión: la operación pudo haberse hecho del lado del exchange."""
    try:
        import ccxt
        if isinstance(e, ccxt.NetworkError):
            return True
        if isinstance(e, ccxt.BaseError):
            return False
    except ImportError:
        pass
    return isinstance(e, (TimeoutError, ConnectionError, OSError))


def planificar(E_main, libre_main, sub_usdt, sub_btc_usd, peso):
    """Qué hacer con la subcuenta plana. Devuelve dict(accion: 'nada'|'enviar'|'traer', monto, objetivo, E_sub, motivo)."""
    E_sub = sub_usdt + sub_btc_usd
    objetivo = peso * (E_main + E_sub)
    dif = objetivo - E_sub
    out = dict(accion="nada", monto=0.0, objetivo=objetivo, E_sub=E_sub, motivo="")
    if abs(dif) < max(MINIMO_USDT, MINIMO_REL * objetivo):
        out["motivo"] = "diferencia chica"
        return out
    if dif > 0:
        disponible = libre_main - COLCHON_PRINCIPAL * E_main
        monto = min(dif, disponible)
        if monto < MINIMO_USDT:
            out["motivo"] = f"la principal no tiene margen libre suficiente (libre {libre_main:.2f}, falta {dif:.2f})"
            return out
        out.update(accion="enviar", monto=round(monto, 2),
                   motivo="" if monto >= dif - 0.01 else f"parcial: la principal conserva su colchón (faltan {dif - monto:.2f})")
        return out
    monto = min(-dif, sub_usdt)
    if monto < MINIMO_USDT:
        out["motivo"] = "la subcuenta no tiene USDT libre para devolver"
        return out
    out.update(accion="traer", monto=round(monto, 2))
    return out


class TransferidorSubcuenta:
    """Transferencias de USDT entre la principal y la subcuenta de 30 balas.
    spot_principal: ccxt.kucoin con la clave de la principal (permiso FlexTransfers); ejecutor_sub: EjecutorRealBalas;
    db: base del servicio (registro durable de cada transferencia)."""
    ESPERAS = (1, 2, 4, 8)          # segundos entre lecturas de saldo para confirmar un paso dudoso

    def __init__(self, sub_uid, ejecutor_sub, cred_principal=None, spot_principal=None, db=None):
        if not sub_uid:
            raise ValueError("Falta KUCOIN_BALAS_UID (UID de la subcuenta de 30 balas) en .env")
        if spot_principal is None:
            import ccxt
            spot_principal = ccxt.kucoin({**cred_principal, "enableRateLimit": True})
        self.m = spot_principal
        self.sub = ejecutor_sub
        self.uid = str(sub_uid)
        self.db = db
        self._mem = {}                  # registro en memoria si no hay base (pruebas)
        self.ruta_ida = None
        self.ruta_vuelta = None

    # ------------------------------------------------------------ saldos
    def saldo_sub(self, px_btc):
        """USDT (main + trade) y BTC (spot + futuros, en USD) de la subcuenta."""
        s = self.sub.saldos()
        usdt = btc = 0.0
        for k in ("spot_main", "spot_trade"):
            if isinstance(s.get(k), dict):
                usdt += s[k].get("USDT", 0.0); btc += s[k].get("BTC", 0.0)
            else:
                raise RuntimeError(f"No pude leer el saldo {k} de la subcuenta: {s.get(k)}")
        f = s.get("futuros_BTC")
        if isinstance(f, dict):
            btc += f.get("total", 0.0)
        else:
            raise RuntimeError(f"No pude leer el saldo de futuros de la subcuenta: {f}")
        return usdt, btc * px_btc

    def _saldo(self, cuenta):
        """USDT de la subcuenta (main + trade) o de la cuenta «main» de la principal."""
        if cuenta == "sub":
            return self.saldo_sub(0.0)[0]
        b = self.m.fetch_balance({"type": "main"})
        return float((b.get("total") or b.get("free") or {}).get("USDT") or 0.0)

    # ------------------------------------------------------------ registro durable
    def _guardar(self, tid, **campos):
        if self.db is None:
            self._mem.setdefault(tid, dict(id=tid)).update(campos)
            return
        if not self.db.filas("SELECT 1 FROM transferencias WHERE id=?", (tid,)):
            self.db.ejec("INSERT INTO transferencias (id, ts) VALUES (?, ?)", (tid, int(time.time() * 1000)))
        for k, v in campos.items():
            self.db.ejec(f"UPDATE transferencias SET {k}=? WHERE id=?", (json.dumps(v) if k == "pasos" else v, tid))

    def registro(self):
        if self.db is None:
            return [dict(r, pasos=json.dumps(r.get("pasos", []))) for r in self._mem.values()]
        return self.db.filas("SELECT * FROM transferencias ORDER BY ts")

    def pendientes(self):
        return [r for r in self.registro() if r.get("estado") in ("enviando", "incierta", "revisar")]

    # ------------------------------------------------------------ pasos
    def _confirmar(self, cuenta, antes, delta):
        """Después de un error dudoso: True si el saldo de `cuenta` cambió `delta` desde `antes`; False si no cambió;
        None si cambió otra cosa o no se pudo leer."""
        ahora = None
        for s in self.ESPERAS:
            time.sleep(s)
            try:
                ahora = self._saldo(cuenta)
            except Exception:
                continue
            if abs(ahora - antes - delta) <= TOL_USDT:
                return True
        if ahora is None:
            return None
        return False if abs(ahora - antes) <= TOL_USDT else None

    def _paso(self, tid, pasos, desc, fn, cuenta, delta, coid, k=0, de=1, deshace=False):
        """Un paso de una ruta (k de `de`). Se anota antes de mandarlo, con el saldo que se va a mirar."""
        antes = self._saldo(cuenta)
        reg = dict(paso=desc, cuenta=cuenta, antes=antes, delta=delta, clientOid=coid, k=k, de=de, deshace=deshace,
                   estado="enviando")
        pasos.append(reg); self._guardar(tid, pasos=pasos)
        try:
            fn(coid)
        except Exception as e:
            reg["error"] = str(e)[:200]
            if not es_error_de_red(e):
                reg["estado"] = "rechazada"; self._guardar(tid, pasos=pasos)
                raise _Rechazada(f"{desc}: {str(e)[:160]}")
            if self._confirmar(cuenta, antes, delta):
                reg["estado"] = "hecha (confirmada por saldo)"; self._guardar(tid, pasos=pasos)
                log.warning("transferencia %s: %s sin respuesta pero confirmada por saldo", tid[:8], desc)
                return
            reg["estado"] = "incierta"; self._guardar(tid, estado="incierta", pasos=pasos, error=str(e)[:300])
            raise TransferenciaIncierta(f"{desc}: sin respuesta ({str(e)[:120]}) y el saldo no lo confirma; "
                                        "no se reintenta ni se prueba otra ruta hasta resolverla con los saldos")
        reg["estado"] = "hecha"; self._guardar(tid, pasos=pasos)

    def _transferir(self, accion, monto, rutas, preferida):
        """rutas: [(nombre, [(desc, fn(clientOid), cuenta a mirar, delta esperado)], deshacer | None)]."""
        tid = uuid.uuid4().hex
        self._guardar(tid, accion=accion, monto=monto, estado="enviando", sub_antes=self._saldo("sub"), pasos=[])
        if preferida:
            rutas = [x for x in rutas if x[0] == preferida] + [x for x in rutas if x[0] != preferida]
        errores, reg = [], []
        for i, (nombre, pasos, deshacer) in enumerate(rutas):
            self._guardar(tid, ruta=nombre)
            n0 = len(reg)
            try:
                for k, (desc, fn, cuenta, delta) in enumerate(pasos):
                    self._paso(tid, reg, desc, fn, cuenta, delta, f"{tid[:26]}r{i}p{k}", k, len(pasos))
            except _Rechazada as e:
                hechos = [r for r in reg[n0:] if r["estado"].startswith("hecha")]
                if hechos and deshacer:          # el segundo paso se rechazó: se deshace el primero (también verificado)
                    try:
                        self._paso(tid, reg, *deshacer, f"{tid[:26]}r{i}u", 0, 1, True)
                    except _Rechazada as e2:
                        self._guardar(tid, estado="revisar", pasos=reg, error=f"{e}; no pude deshacer: {e2}"[:300])
                        raise RuntimeError(f"segundo paso rechazado ({e}) y no pude deshacer el primero ({e2}): revisar a mano")
                    errores.append(f"{nombre}: {e} (primer paso deshecho)")
                    continue
                if hechos:
                    self._guardar(tid, estado="revisar", pasos=reg, error=str(e)[:300])
                    raise RuntimeError(f"segundo paso rechazado ({e}); el USDT quedó en la cuenta «main» de la principal: revisar")
                errores.append(str(e))
                continue
            self._guardar(tid, estado="hecha", ruta=nombre, pasos=reg, sub_despues=self._saldo("sub"))
            log.info("transferencia %s %s %.2f USDT por %s", tid[:8], accion, monto, nombre)
            return nombre
        self._guardar(tid, estado="fallida", error="; ".join(errores)[:300])
        raise RuntimeError(f"No pude transferir ({accion} {monto:.2f} USDT). Intentos:\n  " + "\n  ".join(errores))

    def _flex(self, monto, desde, hacia, tipo, coid):
        p = {"transferType": tipo, "clientOid": coid}
        if tipo == "PARENT_TO_SUB":
            p["toUserId"] = self.uid
        elif tipo == "SUB_TO_PARENT":
            p["fromUserId"] = self.uid
        return self.m.transfer("USDT", monto, desde, hacia, p)

    def enviar(self, monto):
        """Principal (futuros) → subcuenta (main) y, dentro de la subcuenta, main → trade."""
        f = self._flex
        rutas = [
            ("futuros principal → main subcuenta",
             [("futuros principal → main subcuenta", lambda c: f(monto, "contract", "main", "PARENT_TO_SUB", c), "sub", monto)], None),
            ("futuros → main principal, luego main → main subcuenta",
             [("futuros → main principal", lambda c: f(monto, "contract", "main", "INTERNAL", c), "principal", monto),
              ("main principal → main subcuenta", lambda c: f(monto, "main", "main", "PARENT_TO_SUB", c), "sub", monto)],
             ("deshacer: main → futuros principal", lambda c: f(monto, "main", "contract", "INTERNAL", c), "principal", -monto)),
        ]
        self.ruta_ida = self._transferir("enviar", monto, rutas, self.ruta_ida)
        time.sleep(1)
        self.sub.usdt_a_trading()
        return self.ruta_ida

    def traer(self, monto):
        """Subcuenta (trade → main) → principal (futuros)."""
        b = self.sub.spot.fetch_balance({"type": "trade"})
        en_trade = float(b["free"].get("USDT") or 0)
        if en_trade > 0.01:
            self.sub.spot.transfer("USDT", min(en_trade, monto), "trade", "main")      # dentro de la subcuenta
            time.sleep(1)
        f = self._flex
        rutas = [
            ("main subcuenta → futuros principal",
             [("main subcuenta → futuros principal", lambda c: f(monto, "main", "contract", "SUB_TO_PARENT", c), "sub", -monto)], None),
            ("main subcuenta → main principal, luego main → futuros principal",
             [("main subcuenta → main principal", lambda c: f(monto, "main", "main", "SUB_TO_PARENT", c), "sub", -monto),
              ("main → futuros principal", lambda c: f(monto, "main", "contract", "INTERNAL", c), "principal", -monto)], None),
        ]
        self.ruta_vuelta = self._transferir("traer", monto, rutas, self.ruta_vuelta)
        return self.ruta_vuelta

    # ------------------------------------------------------------ transferencias que quedaron inciertas
    def resolver(self):
        """Decide con los saldos las transferencias que quedaron inciertas (o a medias por una caída del proceso).
        Devuelve las que siguen sin resolver: mientras haya alguna, no se transfiere y 30 balas no abre campaña.
          · el paso dudoso se hizo: si era el último de su ruta, la transferencia está hecha; si era un paso intermedio
            o un «deshacer», queda «revisar» (dinero en la cuenta main de la principal) salvo que el deshacer complete
            la vuelta atrás (entonces no se movió nada: «fallida»);
          · no se hizo: si era el primer paso, no se movió nada («fallida»); si no, «revisar»;
          · el saldo cambió otra cosa: sigue incierta."""
        quedan = []
        for t in self.pendientes():
            pasos = json.loads(t["pasos"]) if isinstance(t.get("pasos"), str) else list(t.get("pasos") or [])
            dudosos = [x for x in pasos if x["estado"] in ("enviando", "incierta")]
            if t["estado"] == "revisar":
                quedan.append(t)
                continue
            if not dudosos:
                if not any(x["estado"].startswith("hecha") for x in pasos):     # cayó antes de mandar nada
                    self._guardar(t["id"], estado="fallida", error="interrumpida antes de transferir")
                else:
                    quedan.append(t)
                continue
            x = dudosos[-1]
            try:
                ahora = self._saldo(x["cuenta"])
            except Exception:
                quedan.append(t)
                continue
            if abs(ahora - x["antes"] - x["delta"]) <= TOL_USDT:
                x["estado"] = "hecha (confirmada después)"
                if x.get("deshace"):
                    estado = "fallida"                       # se deshizo el primer paso: no se movió nada
                else:
                    estado = "hecha" if x["k"] == x["de"] - 1 else "revisar"
            elif abs(ahora - x["antes"]) <= TOL_USDT:
                x["estado"] = "no se hizo"
                estado = "fallida" if x["k"] == 0 and not x.get("deshace") else "revisar"
            else:
                quedan.append(t)
                continue
            self._guardar(t["id"], pasos=pasos, estado=estado)
            if estado == "revisar":
                quedan.append(dict(t, estado="revisar"))
        return quedan
