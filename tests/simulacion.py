"""Prueba de punta a punta sin internet: el servicio completo (ciclos, balas, controles, comandos y web)
sobre velas históricas, con un exchange público falso. Uso igual que tests.paridad."""
import argparse, base64, json, queue, time, urllib.request
import pandas as pd
from cascada import config as C
from cascada.db import Base
from cascada import principal as P
from cascada.principal import Sistema
P.REINTENTOS_VELAS = ()
from cascada import web
from tests.paridad import PublicoFalso, cargar_datos


class Pub(PublicoFalso):
    def velas(self, base, tf, desde, hasta=None):
        return pd.DataFrame(columns=["o", "h", "l", "c", "v"])
    def funding(self, base, simbolo=None):
        return 0.0001

    def funding_liquidado(self, base, desde_ms, simbolo=None):
        """Un evento de 0,01 % cada 8 h (perpetuos y XBTUSDM), como lo devuelve KuCoin: (ts_ms, tasa)."""
        hasta = int(self.t.value // 10**6) if self.t is not None else desde_ms
        paso = 8 * 3600_000
        ts = (desde_ms // paso + 1) * paso
        out = []
        while ts <= hasta:
            out.append((ts, 0.0001)); ts += paso
        return out[:20]


def main(a):
    ruta = "/tmp/sim.db"
    for s in ("", "-wal", "-shm"):
        try: __import__("os").unlink(ruta + s)
        except FileNotFoundError: pass
    db = Base(ruta)
    bases = cargar_datos(db, a.velas, a.top50, a.resumen, a.universo, desde="2024-09-01")
    pub = Pub(db, bases)
    cfg = C.cargar(); cfg.web_clave = "x"; cfg.capital_papel = 3000; cfg.telegram_token = ""
    cfg.desactivadas = ["sold_btc"]; cfg.pesos["sold_btc"] = 0.0
    avisos = []
    s = Sistema(cfg, avisar=lambda n, t: avisos.append((n, t)), publico=pub, ruta_db=ruta)
    s.cola_web = queue.Queue()
    srv = web.arrancar(s, puerto=8099, host="127.0.0.1")
    ts = pd.date_range(a.desde, a.hasta, freq="4h")
    t0 = time.time()
    for k, t in enumerate(ts):
        pub.t = t
        s.ciclo(t)
        # control a mitad de vela con precios de esa vela
        s.db.set("latido", time.time())
        if k % 6 == 3:
            s.control()
    print(f"{len(ts)} ciclos en {time.time()-t0:.0f} s")
    print(s.comando("estado", []))
    print(s.comando("previsiones", []))
    print(s.comando("cerrar_todo", []))
    print(s.comando("cerrar_todo", ["si"]))
    print("posiciones tras cerrar todo:", s.bolsa.posiciones(), "· balas activa:", s.balas.st["activo"] if s.balas else None,
          "· liquidando:", s.db.get("liquidando"))
    print(s.comando("reanudar", []))
    aut = {"Authorization": "Basic " + base64.b64encode(b"admin:x").decode()}
    for u in ("/", "/api/todo", "/api/patrimonio?rango=todo", "/api/operaciones?limite=50", "/api/backtest", "/api/comparacion", "/salud"):
        r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8099" + u, headers=aut))
        b = r.read(); print(u, r.status, len(b))
        if u == "/api/todo":
            d = json.loads(b); print(" kpis", {k: d["kpis"].get(k) for k in ("patrimonio", "rend_total", "caida_max", "rend_mes")})
            print(" estrategias", {k: (v.get("lotes"), round(v.get("pnl") or 0, 1)) for k, v in d["estrategias"].items()})
    try:
        urllib.request.urlopen("http://127.0.0.1:8099/api/todo"); print("¡SIN AUTENTICACIÓN!")
    except urllib.error.HTTPError as e:
        print("sin clave ->", e.code)
    print("avisos:", len(avisos), [x for x in avisos if x[0] in ("critica", "alta")][:8])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--velas"); ap.add_argument("--top50"); ap.add_argument("--resumen"); ap.add_argument("--universo")
    ap.add_argument("--desde", default="2025-12-01"); ap.add_argument("--hasta", default="2026-02-01")
    main(ap.parse_args())
