"""Pruebas sin red del servicio 30 balas 10x:  python -m balas10.pruebas"""
import os
import tempfile

import numpy as np
import pandas as pd

from sistema3.db import Base

from . import config as C
from . import motor as M
from . import senales as S
from .bolsa import PapelInverso

M.time.sleep = lambda s: None
OK = []


def ok(cond, nombre):
    if not cond:
        raise AssertionError(nombre)
    OK.append(nombre)


class TG:
    def __init__(self):
        self.msgs = []
        import queue
        self.comandos = queue.Queue()

    def avisar(self, n, t):
        self.msgs.append((n, t))

    def escuchar(self):
        pass


def senal(**kw):
    base = dict(t="2026-10-12 00:00:00", dia=True, cierre=84000.0, sma=83000.0, sobre=True, sobre_previa=True, rsi=50.0,
                vela="2026-10-11 20:00:00", regimen=True, levm=1.0, momento=True, vol=0.4, mayer=1.2, ret90=0.1,
                ema_sem=80000.0, cierre_dom=84000.0, ultimo_dia="2026-10-11", entrada=False)
    base.update(kw)
    return base


def armar(btc=0.5, precio=84000.0, cfg=None):
    db = Base(os.path.join(tempfile.mkdtemp(), "t.db"))
    px = {"v": precio}
    b = PapelInverso(db, lambda: px["v"], btc=btc, desliz=0.0, com=0.0)
    cfg = cfg or C.Config(modo="papel", confirmar=False)
    av = []
    m = M.Motor(cfg, db, b, fuente=None, avisar=lambda n, t: av.append((n, t)))
    return db, b, m, px, av


def prueba_senales():
    rng = np.random.default_rng(0)
    idx = pd.date_range("2023-01-01", periods=6 * 900, freq="4h")
    c4 = pd.Series(30000 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, len(idx)))), index=idx)
    d = c4[(c4.index + pd.Timedelta(hours=4)).hour == 0]; d.index = d.index.normalize()
    t = idx[-1] + pd.Timedelta(hours=4)
    s = S.senal(c4, d[d.index + pd.Timedelta(days=1) <= t], t)
    ok(set(["regimen", "levm", "momento", "rsi", "sma", "entrada"]) <= set(s), "señales: campos")
    ok(0.33 * 0.5 - 1e-9 <= s["levm"] <= 1.0, "señales: multiplicador en rango")
    ok(S.balas_por_roe(0.01) == (1, 1.0) and S.balas_por_roe(-0.12) == (6, 0.7) and S.balas_por_roe(-0.4) == (8, 0.0),
       "señales: tabla de recarga")


def prueba_adopcion_y_reserva():
    # tu caso: 0,25 en posición (30 balas de ~7000 contratos), 0,25 de reserva en futuros, 0,125 afuera
    db, b, m, px, av = armar(btc=0.25 + 0.25, precio=83102.7)
    b.comprar(223481, 10)                                  # toma 0,2689 BTC de margen inicial
    b.st["libre"] = 0.25; b.st["pos"]["m"] = 0.2653; b._g()
    px["v"] = 83859.4
    s, pos = m.adoptar(0.25, 0.25, 0.125, 30, "2026-10-01", rec=True)
    ok(s["activo"] and m.usadas(s) == (30, 0), "adopción: 30 balas usadas del cargador 1")
    liq0 = b.posicion()["liq"]
    inf = m.control()
    p = b.posicion()
    ok(abs(p["margen"] - (0.2653 + 0.125)) < 1e-6 and m.st["ini_puesta"], "adopción: entra la mitad de la reserva")
    ok(p["liq"] < liq0 and p["marca"] / p["liq"] - 1 > 0.12, "adopción: la liquidación se aleja a más de 12 %")
    ok(not m.st["resd"], "adopción: el resto de la reserva queda en espera")
    # caída hasta quedar a menos de 12 % de la liquidación
    px["v"] = p["liq"] * 1.10
    m.control()
    ok(m.st["resd"] and abs(b.posicion()["margen"] - (0.2653 + 0.25)) < 1e-6, "reserva: entra el resto al 12 %")
    ok(any("resto de la reserva" in t for _, t in av), "reserva: avisa por Telegram")


def prueba_cargador2_afuera():
    db, b, m, px, av = armar(btc=0.5, precio=84000)
    b.comprar(210000, 10)
    m.adoptar(0.25, 0.25, 0.125, 30, "2026-10-01", rec=True)
    m.control()                                            # reserva inicial
    b.st["libre"] = 0.125; b._g()                          # sólo queda el resto de la reserva
    m.senales = lambda t: senal()                          # día, régimen activo, sin salida
    px["v"] = 90000                                        # ROE positivo → 1 bala, del cargador 2 (el 1 está agotado)
    inf, dec = m.ciclo(pd.Timestamp("2026-10-12"))
    s = m.st
    ok(dec is None and s["c2_debe"] > 0 and abs(s["c2_debe"] - 0.125 / 30) < 1e-9, "cargador 2: queda la deuda de 1 bala")
    ok(abs(b.st["libre"] - 0.125) < 1e-9, "cargador 2: no toca la reserva")
    ok(any("cargador 2" in t for n, t in av if n == "alta"), "cargador 2: pide el depósito")
    b.depositar(0.125)
    m.control()
    s = m.st
    ok(s["c2_debe"] < 1e-9 and abs(s["c2_usado"] - 0.125 / 30) < 1e-9, "cargador 2: al depositar entra solo")
    ok(abs(s["afuera"] - (0.125 - 0.125 / 30)) < 1e-9, "cargador 2: descuenta lo que estaba afuera")


def prueba_salida_y_entrada():
    db, b, m, px, av = armar(btc=1.0, precio=80000)
    m.senales = lambda t: senal(dia=False, entrada=True, rsi=10, sobre=False)
    inf, dec = m.ciclo(pd.Timestamp("2026-10-12 04:00"))
    ok(dec and dec["tipo"] == "entrada", "entrada: pide confirmación para abrir")
    inf = m.ejecutar(dec)
    s = m.st
    p = b.posicion()
    ok(s["activo"] and abs(s["Wc"] - 1.0) < 1e-6, "entrada: capital de la campaña = BTC en futuros")
    ok(abs(p["margen"] - (0.4 / 30 + 0.4 * 0.5)) < 1e-6, "entrada: 1 bala + mitad de la reserva como margen")
    ok(abs(p["contratos"] - round(0.4 / 30 * 10 * 80000)) <= 1, "entrada: contratos = bala × 10x × precio")
    # sube sobre la SMA y luego cruza abajo → salida
    m.senales = lambda t: senal(dia=False, sobre=True, sobre_previa=False)
    m.ciclo(pd.Timestamp("2026-10-12 08:00"))
    ok(m.st["rec"], "salida: registra que cruzó sobre la SMA80")
    m.senales = lambda t: senal(dia=False, sobre=False, sobre_previa=True)
    inf, dec = m.ciclo(pd.Timestamp("2026-10-12 12:00"))
    ok(dec and dec["tipo"] == "salida", "salida: cruce bajo la SMA80 pide cerrar")
    px["v"] = 88000
    m.ejecutar(dec)
    ok(b.posicion() is None and not m.st["activo"] and m.st["campanas"] == 1, "salida: posición cerrada")
    ok(b.cuenta()["equity"] > 1.0, "salida: ganancia en BTC")
    # régimen apagado al cierre diario también cierra
    m.senales = lambda t: senal(dia=False, entrada=True)
    m.ejecutar(m.ciclo(pd.Timestamp("2026-10-13 04:00"))[1])
    m.senales = lambda t: senal(dia=True, regimen=False)
    inf, dec = m.ciclo(pd.Timestamp("2026-10-14 00:00"))
    ok(dec and dec["tipo"] == "salida" and "régimen" in dec["motivo"], "salida: régimen apagado")


def prueba_recarga_con_ok():
    db, b, m, px, av = armar(btc=1.0, precio=80000)
    m.senales = lambda t: senal(dia=False, entrada=True)
    m.ejecutar(m.ciclo(pd.Timestamp("2026-10-12 04:00"))[1])
    m.control()
    px["v"] = 60000                                         # ROE ≈ −21 % → 8 balas, 40 % a posición
    m.senales = lambda t: senal(dia=True, sobre=False, sobre_previa=False)
    c0 = b.posicion()["contratos"]
    inf, dec = m.ciclo(pd.Timestamp("2026-10-13 00:00"))
    ok(dec and dec["tipo"] == "recarga", "recarga: compra de contratos pide confirmación")
    ok(any("sólo margen" in x for x in inf), "recarga: la parte sólo-margen sale sola")
    ok(b.posicion()["contratos"] == c0, "recarga: no compra antes del OK")
    m.ejecutar(dec)
    ok(b.posicion()["contratos"] > c0, "recarga: compra al aprobar")


def prueba_conciliacion():
    db, b, m, px, av = armar(btc=0.5, precio=84000)
    b.comprar(100000, 10)
    m.adoptar(0.2, 0.2, 0.1, 30, "2026-10-01", rec=True)
    b.st["pos"] = None; b._g()                              # la posición desaparece (liquidación o cierre a mano)
    m.senales = lambda t: senal()
    inf, dec = m.ciclo(pd.Timestamp("2026-10-12"))
    ok(dec is None and not m.st["activo"] and any(n == "critica" for n, _ in av), "conciliación: posición desaparecida")
    db2, b2, m2, px2, av2 = armar(btc=0.5)
    b2.comprar(100000, 10)
    m2.senales = lambda t: senal(entrada=True)
    inf, dec = m2.ciclo(pd.Timestamp("2026-10-12"))
    ok(dec is None and any("no conoce" in t for _, t in av2), "conciliación: posición sin adoptar no se toca")


def prueba_emergencia():
    db, b, m, px, av = armar(btc=0.6, precio=84000)
    b.comprar(210000, 10)
    m.adoptar(0.25, 0.25, 0.125, 30, "2026-10-01", rec=True)
    m.control()
    px["v"] = b.posicion()["liq"] * 1.11
    m.control()                                             # resto de la reserva
    px["v"] = b.posicion()["liq"] * 1.04
    libre = b.st["libre"]
    m.control()
    ok(any(n == "critica" for n, _ in av), "emergencia: aviso crítico a ≤ 5 %")
    ok(b.st["libre"] < libre and b.st["libre"] < 2e-5, "emergencia: todo el BTC libre entra como margen")


def prueba_servicio_confirmacion():
    from .principal import Servicio
    tmp = tempfile.mkdtemp()
    cfg = C.Config(modo="real", confirmar=True)
    cfg.telegram_escuchar = True
    db = Base(os.path.join(tmp, "s.db"))
    px = {"v": 80000.0}
    b = PapelInverso(db, lambda: px["v"], btc=1.0, desliz=0.0, com=0.0)
    tg = TG()
    s = Servicio(cfg=cfg, bolsa=b, fuente=None, ruta_db=os.path.join(tmp, "s.db"), telegram=tg)
    s.motor.senales = lambda t: senal(dia=False, entrada=True)
    s.ciclo(pd.Timestamp("2026-10-12 04:00"))
    p = s.motor.pendiente()
    ok(p and p["tipo"] == "entrada" and b.posicion() is None, "servicio: queda pendiente sin operar")
    r = s.comando("si", ["xxxx"])
    ok("La pendiente es" in r and b.posicion() is None, "servicio: /si con otro ID no ejecuta")
    s._aprobada(p)
    ok(b.posicion() is not None and s.motor.decision(p["id"])["estado"] == "ejecutada", "servicio: /si ejecuta")
    s.motor.senales = lambda t: senal(dia=False, sobre=False, sobre_previa=True)
    st = s.motor.st; st["rec"] = True; s.motor.guardar(st)
    s.ciclo(pd.Timestamp("2026-10-12 08:00"))
    p = s.motor.pendiente()
    r = s.comando("no", [p["id"]])
    ok("descartada" in r and b.posicion() is not None, "servicio: /no no cierra")
    r = s.comando("afuera", ["0,1"])
    ok(abs(s.motor.st["afuera"] - 0.1) < 1e-12, "servicio: /afuera")


def main():
    for f in (prueba_senales, prueba_adopcion_y_reserva, prueba_cargador2_afuera, prueba_salida_y_entrada,
              prueba_recarga_con_ok, prueba_conciliacion, prueba_emergencia, prueba_servicio_confirmacion):
        f()
    print(f"{len(OK)} comprobaciones OK")
    for x in OK:
        print("  ✓", x)


if __name__ == "__main__":
    main()
