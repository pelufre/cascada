"""Pruebas del Sistema 3 sin red: fuente y exchange simulados.   python -m sistema3.pruebas"""
import math
import tempfile
import time

import numpy as np
import pandas as pd

from . import senales as S
from .bolsa import Papel
from .config import Config
from .db import Base
from .motor import Motor

HOY = pd.Timestamp("2026-10-09")          # 00:00 UTC: la última vela diaria cerrada es la del 08/10


class FuenteFalsa:
    """Series diarias y de 4 h armadas a mano. tendencias: {sim: retorno diario}; precio inicial 100 (BTC 50.000)."""

    def __init__(self, diarios, h4=None):
        self.d = diarios            # {sim: DataFrame o,h,l,c,qv}
        self.h4 = h4 or {}

    def mercados_spot(self):
        return {s: s + "USDT" for s in self.d}

    def mercados_perp(self):
        return {s: dict(sym=s + "USDT", desde_ms=0) for s in self.h4}

    def diarias(self, s, n=400):
        return self.d.get(s)

    def perp_4h(self, s, n=1000):
        return self.h4.get(s)

    def precio_spot(self, s):
        return float(self.d[s]["c"].iloc[-1])


def serie_diaria(r, n=400, p0=100.0, vol=0.01, semilla=0, fin=HOY - pd.Timedelta(days=1), qv=5e7, quiebre=None):
    rng = np.random.default_rng(semilla)
    rets = np.full(n, r) + rng.normal(0, vol, n)
    if quiebre:
        k, r2 = quiebre
        rets[-k:] = r2 + rng.normal(0, vol, k)
    c = p0 * np.cumprod(1 + rets)
    o = np.r_[p0, c[:-1]]
    idx = pd.date_range(end=fin, periods=n, freq="D")
    return pd.DataFrame(dict(o=o, h=np.maximum(o, c) * 1.01, l=np.minimum(o, c) * 0.99, c=c, qv=qv), index=idx)


def serie_4h(r, n=1000, p0=100.0, vol=0.004, semilla=0, fin=HOY - pd.Timedelta(hours=4), quiebre=None, qv=2e7):
    d = serie_diaria(r, n, p0, vol, semilla, fin, qv, quiebre)
    d.index = pd.date_range(end=fin, periods=n, freq="4h")
    return d


def armar(diarios, h4=None, nivel="moderado", capital=10_000.0):
    db = Base(tempfile.mktemp(suffix=".db"))
    f = FuenteFalsa(diarios, h4)
    lunes = (HOY - pd.Timedelta(days=HOY.dayofweek)).strftime("%Y-%m-%d")
    for i, s in enumerate(sorted(set(diarios) | set(h4 or {}))):
        db.ejec("INSERT INTO universo VALUES (?,?,?)", (lunes, s, i + 1))

    def precio(s, tipo):
        if tipo == "fut" and s in f.h4:
            return float(f.h4[s]["c"].iloc[-1])
        return f.precio_spot(s)
    b = Papel(db, precio, capital=capital, desliz=0.0)
    cfg = Config(modo="papel", nivel=nivel, confirmar=False)
    avisos = []
    m = Motor(cfg, db, f, b, avisar=lambda n, t: avisos.append((n, t)))
    return m, f, b, avisos


def ok(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ✓", msg)


def prueba_indicadores():
    print("Indicadores")
    d = serie_diaria(0.001, 60)
    a = S.atr_wilder(d.h, d.l, d.c)
    tr = [d.h.iloc[0] - d.l.iloc[0]]
    for i in range(1, len(d)):
        tr.append(max(d.h.iloc[i] - d.l.iloc[i], abs(d.h.iloc[i] - d.c.iloc[i - 1]), abs(d.l.iloc[i] - d.c.iloc[i - 1])))
    m = tr[0]
    for x in tr[1:]:
        m += (x - m) / 14
    ok(abs(a.iloc[-1] - m) < 1e-9, "ATR14 de Wilder coincide con el cálculo a mano")
    amp = pd.Series([0.6, 0.75, 0.6, 0.55, 0.45, 0.65, 0.72], index=pd.date_range("2026-01-01", periods=7))
    e = S.estado_amplitud(amp)
    ok(list(e) == [False, True, True, True, False, False, True], "histéresis 70/50 de la amplitud")
    e2 = S.estado_amplitud(amp, previo=True, desde=amp.index[0])
    ok(bool(e2.iloc[0]) is True, "la histéresis continúa desde el estado guardado")


def prueba_modo_btc():
    print("Modo BTC (BTC sube, las alts no acompañan)")
    D = {"BTC": serie_diaria(0.004, p0=50_000, semilla=1)}
    for i in range(12):
        D[f"A{i}"] = serie_diaria(0.0, semilla=10 + i, quiebre=(60, -0.004))
    m, f, b, av = armar(D)
    A = m.decidir_diaria(HOY)
    ok(A["modo"] == "BTC", f"modo BTC (amplitud {A['info']['amplitud']:.0%})")
    inf = m.ejecutar(dict(A=A))
    L = m.libro
    v = m.valuar()
    ok(L["btc"] and abs(v["largos"] / v["T"] - 0.999) < 0.002, f"BTC ≈ 100 % del capital ({v['largos'] / v['T']:.1%})")
    inf2 = m.ejecutar(dict(A=A))
    ok(not inf2, "con BTC ya comprado no se rebalancea")
    m.cfg.nivel = "conservador"
    m.ejecutar(dict(A=dict(A, modo="USDT")))
    ok(not m.libro["btc"], "modo USDT vende todo")
    m.ejecutar(dict(A=A))
    v = m.valuar()
    ok(abs(v["largos"] / v["T"] - 0.75) < 0.005, f"nivel conservador: BTC al 75 % ({v['largos'] / v['T']:.1%})")


def prueba_modo_alts():
    print("Modo ALTS (todo sube, las alts más que BTC)")
    D = {"BTC": serie_diaria(0.003, p0=50_000, semilla=1)}
    for i in range(14):
        d = serie_diaria(0.004 + 0.0005 * i, semilla=20 + i, vol=0.02)
        d["h"] *= 1.03; d["l"] *= 0.97                            # ATR% ≈ 8 %: pesos por debajo del tope de 10 %
        D[f"A{i}"] = d
    D["STB"] = serie_diaria(0.006, semilla=99, qv=1e6)            # sin liquidez: no es elegible
    m, f, b, av = armar(D)
    A = m.decidir_diaria(HOY)
    ok(A["modo"] == "ALTS", f"modo ALTS (amplitud {A['info']['amplitud']:.0%})")
    ok(len(A["candidatas"]) == 10, "10 candidatas (cupos llenos)")
    ok("STB" not in [x["sim"] for x in A["candidatas"]], "una moneda con volumen < 2 M no es elegible")
    r20 = [x["roc20"] for x in A["candidatas"]]
    ok(r20 == sorted(r20, reverse=True), "ordenadas por ROC20 del par alt/BTC")
    ok(all(abs(x["peso"] - min(0.10, 0.005 / x["atrp"])) < 1e-12 for x in A["candidatas"]), "peso = mín(10 %; 0,50 % ÷ ATR%)")
    m.ejecutar(dict(A=A))
    L = m.libro
    ok(len(L["alts"]) == 10 and not L["btc"], "compró 10 alts y nada de BTC")
    v = m.valuar()
    esperado = sum(x["peso"] for x in A["candidatas"])
    ok(abs(v["largos"] / v["T"] - esperado) < 0.01, f"exposición {v['largos'] / v['T']:.1%} ≈ suma de pesos {esperado:.1%}")
    # salida: una alt cuyo par cae bajo su SMA20
    s0 = A["candidatas"][0]["sim"]
    d = f.d[s0].copy(); d.iloc[-1, d.columns.get_loc("c")] *= 0.6; f.d[s0] = d
    A2 = m.decidir_diaria(HOY)
    ok(s0 in A2["ventas"], f"{s0} sale al cerrar el par bajo la SMA20")
    m.ejecutar(dict(A=A2))
    ok(s0 not in m.libro["alts"], "venta ejecutada y registrada")
    ok(m.db.filas("SELECT COUNT(*) n FROM cerradas")[0]["n"] == 1, "operación cerrada en el historial")


def prueba_cortos_y_conflicto():
    print("Cortos Aberration y regla de conflicto")
    D = {"BTC": serie_diaria(0.004, p0=50_000, semilla=1)}
    for i in range(12):
        D[f"A{i}"] = serie_diaria(0.0, semilla=10 + i, quiebre=(60, -0.004))
    H = {"BTC": serie_4h(0.0, p0=50_000, semilla=2, quiebre=(500, -0.0006))}
    for i in range(8):                                           # caen fuerte en las últimas velas: bajo la banda
        H[f"S{i}"] = serie_4h(0.0, semilla=30 + i, quiebre=(560, -0.0012 - 0.0002 * i))
        d = H[f"S{i}"].copy(); d.iloc[-1, d.columns.get_loc("c")] = d.c.iloc[-2] * 0.9; d.iloc[-1, d.columns.get_loc("o")] = d.c.iloc[-2]
        H[f"S{i}"] = d
    m, f, b, av = armar(D, H)
    A = m.decidir_diaria(HOY)
    B = m.decidir_4h(HOY)
    ok(B["regimen"], f"régimen de cortos activo (ROC90 BTC {B['roc540_btc']:+.1%})")
    ok(len(B["candidatas"]) >= 5, f"{len(B['candidatas'])} candidatas bajo la banda")
    rr = [x["roc540"] for x in B["candidatas"]]
    ok(rr == sorted(rr), "ordenadas de la más débil a la menos débil")
    # primero BTC (modo BTC), después aparecen los cortos → conflicto
    m.ejecutar(dict(A=A))
    T0 = m.valuar()["T"]
    m.ejecutar(dict(B=B))
    L = m.libro; v = m.valuar()
    ok(L["conflicto"], "largos y cortos a la vez → conflicto")
    ok(abs(v["largos"] / v["T"] - 0.5) < 0.01, f"BTC a la mitad ({v['largos'] / v['T']:.1%})")
    ok(len(L["cortos"]) == 5, "5 cortos abiertos")
    ok(abs(v["nocional_cortos"] / v["T"] - 0.25) < 0.02, f"cortos al 5 % c/u ({v['nocional_cortos'] / v['T']:.1%} en total)")
    ok(v["futuros"] >= 0.6 * v["nocional_cortos"] - 1, "margen de FUTUROS ≥ 60 % del nocional")
    # salen todos los cortos → fin del conflicto: BTC vuelve al 100 %
    m.ejecutar(dict(B=dict(B, salidas=list(L["cortos"]), candidatas=[])))
    L = m.libro; v = m.valuar()
    ok(not L["cortos"] and not L["conflicto"], "cortos cerrados, conflicto terminado")
    ok(abs(v["largos"] / v["T"] - 0.999) < 0.005, f"BTC devuelto a su tamaño normal ({v['largos'] / v['T']:.1%})")
    ok(b.patrimonio_futuros() < 1, "sin cortos, el saldo de FUTUROS vuelve a SPOT")
    # al revés: cortos abiertos y los largos quieren BTC → mitad y mitad; al salir los largos, cortos al 10 %
    m.ejecutar(dict(A=dict(A, modo="USDT")))
    m.ejecutar(dict(B=B))
    ok(len(m.libro["cortos"]) == 5 and not m.libro["conflicto"], "5 cortos sin conflicto (largos en USDT)")
    v = m.valuar(); ok(abs(v["nocional_cortos"] / v["T"] - 0.5) < 0.03, f"cortos al 10 % c/u ({v['nocional_cortos'] / v['T']:.1%})")
    m.ejecutar(dict(A=A))
    v = m.valuar(); L = m.libro
    ok(L["conflicto"] and abs(v["largos"] / v["T"] - 0.5) < 0.02, f"BTC entra a la mitad ({v['largos'] / v['T']:.1%})")
    ok(abs(v["nocional_cortos"] / v["T"] - 0.25) < 0.03, f"cortos reducidos a la mitad ({v['nocional_cortos'] / v['T']:.1%})")
    m.ejecutar(dict(A=dict(A, modo="USDT")))
    v = m.valuar(); L = m.libro
    ok(not L["conflicto"] and not L["btc"], "largos a USDT → termina el conflicto")
    ok(abs(v["nocional_cortos"] / v["T"] - 0.5) < 0.03, f"cortos devueltos al 10 % c/u ({v['nocional_cortos'] / v['T']:.1%})")
    ok(not m.decidir_4h(HOY)["salidas"], "sin cierres sobre la SMA120 en la última vela")


def prueba_confirmacion():
    print("Confirmación por Telegram")
    from .principal import Servicio

    class TG:
        activo = False
        def __init__(self):
            self.msgs = []
            import queue; self.comandos = queue.Queue()
        def avisar(self, n, t): self.msgs.append((n, t))
        def escuchar(self): pass
    D = {"BTC": serie_diaria(0.004, p0=50_000, semilla=1)}
    for i in range(12):
        D[f"A{i}"] = serie_diaria(0.0, semilla=10 + i, quiebre=(60, -0.004))
    m, f, b, _ = armar(D, {"BTC": serie_4h(0.0005, p0=50_000, semilla=3)})
    tg = TG()
    sv = Servicio.__new__(Servicio)
    import threading, queue
    sv.cfg = Config(modo="real", nivel="moderado", confirmar=True); sv.db = m.db; sv.tg = tg; sv.fuente = f; sv.bolsa = b
    m.cfg = sv.cfg; m.avisar = tg.avisar; sv.motor = m; sv.cerrojo = threading.RLock(); sv.cola_web = queue.Queue()
    m.actualizar_universo = lambda t=None: False
    sv.ciclo(HOY)
    p = m.pendiente()
    ok(p is not None and "Comprar BTC" in p["resumen"], "la decisión queda pendiente con su resumen")
    ok(any(n == "pregunta" for n, _ in tg.msgs), "se preguntó por Telegram")
    ok(not m.libro["btc"], "no se envió ninguna orden antes del OK")
    r = sv.comando("si", ["zzzz"])
    ok("La pendiente es" in r, "un id equivocado no ejecuta")
    sv.comando("si", [p["id"]])
    time.sleep(1.5)
    with sv.cerrojo:
        pass
    ok(m.libro["btc"] is not None, "con /si se ejecuta")
    ok(m.decision(p["id"])["estado"] == "ejecutada", "y queda registrada como ejecutada")
    # una nueva decisión de 4 h conserva la parte diaria pendiente
    m.db.ejec("UPDATE decisiones SET estado='pendiente', vence_ts=? WHERE id=?", (int(time.time() * 1000) + 3600_000, p["id"]))
    did, dec = m.nueva_decision(HOY + pd.Timedelta(hours=4), None, dict(regimen=False, roc540_btc=0.1, salidas=[], sin_precio=[], candidatas=[], universo=0))
    ok(dec["A"] is not None, "la decisión de las 04:00 mantiene la parte diaria todavía pendiente")


def main():
    prueba_indicadores()
    prueba_modo_btc()
    prueba_modo_alts()
    prueba_cortos_y_conflicto()
    prueba_confirmacion()
    prueba_alineacion()
    print("\nTodas las pruebas pasaron.")



def prueba_alineacion():
    print("Alineación inicial (adoptar lo que hay)")
    D = {"BTC": serie_diaria(0.003, p0=50_000, semilla=1)}
    for i in range(14):
        d = serie_diaria(0.004 + 0.0005 * i, semilla=20 + i, vol=0.02); d["h"] *= 1.03; d["l"] *= 0.97
        D[f"A{i}"] = d
    D["MAL"] = serie_diaria(0.004, semilla=77, quiebre=(25, -0.03))       # en cartera, ya bajo su SMA20
    m, f, b, _ = armar(D, {"BTC": serie_4h(0.0005, p0=50_000, semilla=3)})
    b.st["spot"].update({"A10": 2000 / f.precio_spot("A10"), "MAL": 1500 / f.precio_spot("MAL"), "BTC": 1000 / f.precio_spot("BTC"),
                         "POLVO": 1e-6, "SINPAR": 5.0})
    f.d["SINPAR"] = None
    b.st["spot"]["USDT"] = 6000.0
    P = m.plan_alineacion(HOY)
    txt = m.texto_alineacion(P)
    ok(abs(P["T"] - 10_500) < 50, f"capital = USDT + monedas adoptadas ({P['T']:.0f})")
    ok(any(a == "vender" and s == "MAL" for a, s, *_ in P["pasos"]), "vende la que tiene el par bajo la SMA20")
    ok(any(a == "vender" and s == "BTC" for a, s, *_ in P["pasos"]), "en modo ALTS vende el BTC")
    ok(any(s == "A10" and "ajuste" in mot for a, s, x, mot in P["pasos"]), "ajusta el peso de la que se mantiene")
    compras = [s for a, s, *_ in P["pasos"] if a == "comprar" and s != "A10"]
    ok(len(compras) == 9, f"llena los cupos libres ({len(compras)} compras nuevas + A10)")
    m.ejecutar_alineacion(P)
    v = m.valuar(); L = m.libro
    ok(len(L["alts"]) == 10 and not L["btc"] and "MAL" not in L["alts"], "quedan 10 alts, sin BTC ni MAL")
    print(txt.split("\n")[0])


if __name__ == "__main__":
    main()
