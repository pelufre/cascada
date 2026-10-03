"""Capital de 30 balas en real: reequilibrio entre la cuenta principal y la subcuenta (protocolo E4).

Como en la validación y en el papel: mientras 30 balas no tiene campaña abierta, la subcuenta se iguala a
peso × patrimonio total (principal + subcuenta). Durante una campaña no se toca. Se mueve sólo USDT:
  principal → subcuenta: futuros USDT-M de la principal (CONTRACT) → «main» de la subcuenta → «trade» (donde compra BTC)
  subcuenta → principal: «trade» → «main» de la subcuenta → futuros de la principal

La transferencia entre cuentas la hace la clave de la cuenta PRINCIPAL con el permiso «FlexTransfers» de KuCoin (mueve
fondos sólo entre tus cuentas; no es permiso de retiro; KuCoin exige que la clave esté restringida por IP). Siempre se
transfiere la diferencia entre el objetivo y los saldos leídos en ese momento, así que repetir no duplica.
Probar primero con `cli transferencia-prueba`.
"""
import logging
import time
import uuid

log = logging.getLogger("cascada.subcuenta")
MINIMO_USDT = 10.0          # diferencias menores no se transfieren
MINIMO_REL = 0.01           # ni las menores al 1 % del objetivo
COLCHON_PRINCIPAL = 0.10    # la principal conserva al menos 10 % de su patrimonio libre como margen


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
    spot_principal: ccxt.kucoin con la clave de la principal (permiso FlexTransfers); ejecutor_sub: EjecutorRealBalas."""

    def __init__(self, sub_uid, ejecutor_sub, cred_principal=None, spot_principal=None):
        if not sub_uid:
            raise ValueError("Falta KUCOIN_BALAS_UID (UID de la subcuenta de 30 balas) en .env")
        if spot_principal is None:
            import ccxt
            spot_principal = ccxt.kucoin({**cred_principal, "enableRateLimit": True})
        self.m = spot_principal
        self.sub = ejecutor_sub
        self.uid = str(sub_uid)
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

    # ------------------------------------------------------------ transferencias
    def _probar(self, rutas, nombre):
        errores = []
        for ruta in rutas:
            try:
                ruta[1]()
                log.info("transferencia %s por %s", nombre, ruta[0])
                return ruta
            except Exception as e:
                errores.append(f"{ruta[0]}: {str(e)[:160]}")
        raise RuntimeError(f"No pude transferir ({nombre}). Intentos:\n  " + "\n  ".join(errores))

    @staticmethod
    def _dos_pasos(paso1, paso2, deshacer):
        """Si el segundo paso falla, deshace el primero (no deja USDT varado en una cuenta intermedia)."""
        paso1()
        try:
            paso2()
        except Exception as e:
            if deshacer:
                try:
                    deshacer()
                except Exception as e2:
                    raise RuntimeError(f"segundo paso falló ({e}) y no pude deshacer el primero ({e2}): revisar a mano")
                raise RuntimeError(f"segundo paso falló, primero deshecho: {e}")
            raise RuntimeError(f"segundo paso falló; el USDT quedó en la cuenta «main» de la principal: {e}")

    def _flex(self, monto, desde, hacia, tipo):
        p = {"transferType": tipo, "clientOid": uuid.uuid4().hex}
        if tipo == "PARENT_TO_SUB":
            p["toUserId"] = self.uid
        elif tipo == "SUB_TO_PARENT":
            p["fromUserId"] = self.uid
        return self.m.transfer("USDT", monto, desde, hacia, p)

    def enviar(self, monto):
        """Principal (futuros) → subcuenta (main) y, dentro de la subcuenta, main → trade."""
        rutas = [
            ("futuros principal → main subcuenta",
             lambda: self._flex(monto, "contract", "main", "PARENT_TO_SUB")),
            ("futuros → main principal, luego main → main subcuenta",
             lambda: self._dos_pasos(lambda: self._flex(monto, "contract", "main", "INTERNAL"),
                                     lambda: self._flex(monto, "main", "main", "PARENT_TO_SUB"),
                                     lambda: self._flex(monto, "main", "contract", "INTERNAL"))),
        ]
        if self.ruta_ida:
            rutas = [x for x in rutas if x[0] == self.ruta_ida] + [x for x in rutas if x[0] != self.ruta_ida]
        r = self._probar(rutas, f"principal → subcuenta {monto:.2f} USDT")
        self.ruta_ida = r[0]
        time.sleep(1)
        self.sub.usdt_a_trading()

    def traer(self, monto):
        """Subcuenta (trade → main) → principal (futuros)."""
        b = self.sub.spot.fetch_balance({"type": "trade"})
        en_trade = float(b["free"].get("USDT") or 0)
        if en_trade > 0.01:
            self.sub.spot.transfer("USDT", min(en_trade, monto), "trade", "main")
            time.sleep(1)
        rutas = [
            ("main subcuenta → futuros principal",
             lambda: self._flex(monto, "main", "contract", "SUB_TO_PARENT")),
            ("main subcuenta → main principal, luego main → futuros principal",
             lambda: self._dos_pasos(lambda: self._flex(monto, "main", "main", "SUB_TO_PARENT"),
                                     lambda: self._flex(monto, "main", "contract", "INTERNAL"), None)),
        ]
        if self.ruta_vuelta:
            rutas = [x for x in rutas if x[0] == self.ruta_vuelta] + [x for x in rutas if x[0] != self.ruta_vuelta]
        r = self._probar(rutas, f"subcuenta → principal {monto:.2f} USDT")
        self.ruta_vuelta = r[0]
