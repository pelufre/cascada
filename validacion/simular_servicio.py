"""Simulación del SERVICIO completo (cascada.principal.Sistema) sin internet, sobre las velas del paquete de perpetuos,
y comparación evento por evento con el motor de backtest sobre las mismas velas (validacion.comparar_papel).

A diferencia del backtest, acá corre el código del servidor tal cual: ciclo de 4 h, descarga incremental de velas a la
base, universo semanal, volumen de los perpetuos, funding liquidado en papel, capital de 30 balas por campaña, control de
riesgo cada 12 h, stops del papel y conciliación. Sólo el exchange es simulado (precios = apertura de cada vela).

    CASCADA_ABRIR_OOS=1 python -m validacion.simular_servicio --perp … --top50 … --resumen … --universo … \
        [--desde 2026-09-01] [--hasta 2026-10-01] [--base /tmp/servicio.db]
"""
import argparse
import json
import logging
import os
import time

import pandas as pd

from cascada import config as C
from cascada.principal import Sistema

from . import comparar_papel as CP
from . import datos as D
from .motor_bt import Corrida


class PublicoSimulado:
    """Lo que el servicio le pide a KuCoin, servido desde el paquete (sin ver velas posteriores a `t`)."""

    def __init__(self, M, K, F):
        self.M, self.K, self.F, self.t = M, K, F, None

    def mercados(self, refrescar=False):
        K = self.K
        return {b: dict(tam=float(K.at[b, "tam"]), minimo=float(K.at[b, "minimo"] or 1), id=K.at[b, "id"])
                for b in self.M["c"].columns if b in K.index and K.at[b, "tam"] == K.at[b, "tam"]}

    def velas(self, base, tf, desde, hasta=None):
        if base not in self.M["c"]:
            return pd.DataFrame(columns=list("ohlcv"))
        d = pd.DataFrame({k: self.M[k][base] for k in "ohlcv"}).dropna(subset=["c"])
        a = pd.to_datetime(desde, unit="ms"); b = pd.to_datetime(hasta, unit="ms") if hasta else self.t
        b = min(b, self.t)
        return d[(d.index >= a) & (d.index + pd.Timedelta(hours=4) <= b)].fillna(0.0)

    def precios(self, bases):
        fila = self.M["o"].loc[self.t] if self.t in self.M["o"].index else self.M["c"].loc[:self.t].ffill().iloc[-1]
        return {b: float(fila[b]) for b in bases if b in fila and fila[b] == fila[b]}

    def funding(self, base):
        return 0.0001

    def funding_liquidado(self, base, desde_ms):
        if self.F is None or base not in self.F:
            return []
        s = self.F[base].dropna()
        s = s[(s.index > pd.to_datetime(desde_ms, unit="ms")) & (s.index <= self.t)]
        return [(int(i.value // 10**6), float(v)) for i, v in s.items()]


class VolumenSimulado:
    def __init__(self, M, pub):
        self.v = M["v"]; self.pub = pub; self.fuente = {}

    def diario(self, base, dias=35):
        if base not in self.v:
            return None
        v = self.v[base].groupby(self.v.index.floor("D")).sum(min_count=1).dropna()
        hoy = self.pub.t.normalize()
        return v[(v.index < hoy) & (v.index >= hoy - pd.Timedelta(days=dias))]


def main(a):
    logging.disable(logging.WARNING)
    D.limite("OOS")
    P = D.Paquete(a.perp)
    hasta = pd.Timestamp(a.hasta); desde = pd.Timestamp(a.desde)
    M = P.matrices(hasta); K = P.contratos(); F = P.funding(hasta)
    pub = PublicoSimulado(M, K, F)
    cfg = C.cargar(); cfg.modo = "papel"
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(a.base + suf):
            os.remove(a.base + suf)
    s = Sistema(cfg=cfg, avisar=lambda n, m: None, publico=pub, ruta_db=a.base, publico_control=pub)
    s.volumen = VolumenSimulado(M, pub)
    top = pd.read_csv(a.top50, parse_dates=["fecha"]); res = pd.read_csv(a.resumen); uni = pd.read_csv(a.universo)
    top["col"] = top.cmc_id.map(dict(zip(uni.cmc_id, uni.slug))).map(dict(zip(res.slug, res.symbol)))
    for r in top[(top.fecha >= desde - pd.Timedelta(days=60)) & (top.fecha < hasta) & top.col.notna()].itertuples():
        s.db.ejec("INSERT OR REPLACE INTO universo VALUES (?,?,?,?)", (r.fecha.strftime("%Y-%m-%d"), r.col, int(r.puesto), 0.0))
    import cascada.principal as PR
    PR.REINTENTOS_VELAS = ()
    t0 = time.time(); ts = pd.date_range(desde, hasta - pd.Timedelta(hours=4), freq="4h")
    for t in ts:
        pub.t = t
        s.ciclo(t)
        if t.hour == 12:
            s.control()
    db = s.db
    print(f"Servicio: {len(ts)} ciclos en {time.time() - t0:.0f} s · conciliación {db.get('conciliacion_ok')} · "
          f"libro = exchange: {({L['simbolo']: L['contratos'] for L in s.motor.libro().values()} == {b: abs(c) for b, c in s.bolsa.posiciones().items()})}")
    print("Incidencias:", db.filas("SELECT nivel, tipo, COUNT(*) n FROM incidencias GROUP BY nivel, tipo ORDER BY nivel"))
    # motor de backtest sobre las mismas velas, mismo arranque y configuración, con el mismo funding
    cx, q = CP.leer_servidor(a.base)
    merc = pub.mercados()
    r = Corrida(None, None, None, None, cfg.pesos, modo="OOS", corrida="B", capital=cfg.capital_papel, desde=desde,
                hasta=hasta, log_cada=0, cargar=CP.cargador(cx, desde, hasta, F), mercados=merc, cfg=cfg).correr()
    ops_p = q("SELECT * FROM operaciones WHERE ts>=? AND ts<?", (CP._ms(desde), CP._ms(hasta)))
    ops_m = r.db.filas("SELECT * FROM operaciones WHERE ts>=? AND ts<?", (CP._ms(desde), CP._ms(hasta)))
    u = CP.emparejar(CP.eventos(ops_p), CP.eventos(ops_m))
    mal = u[u.problema != ""]
    pat = pd.DataFrame(q("SELECT ts, patrimonio FROM patrimonio WHERE cuenta='total' ORDER BY ts"))
    pat["t"] = pd.to_datetime(pat.ts, unit="ms")
    E_p = pat[pat.t == pat.t.dt.floor("4h")].set_index("t").patrimonio
    comunes = E_p.index.intersection(r.serie.E.index)
    dif = (E_p.loc[comunes] / r.serie.E.loc[comunes] - 1).abs()
    out = dict(desde=str(desde), hasta=str(hasta), operaciones_servicio=len(ops_p), operaciones_motor=len(ops_m),
               eventos=len(u), diferencias=len(mal), patrimonio_servicio=float(E_p.iat[-1]), patrimonio_motor=float(r.serie.E.iat[-1]),
               dif_patrimonio_max=float(dif.max()), dif_patrimonio_media=float(dif.mean()),
               detalle=json.loads(mal.to_json(orient="records", date_format="iso")))
    print(json.dumps({k: v for k, v in out.items() if k != "detalle"}, indent=1))
    if len(mal):
        print(mal.to_string(index=False))
    if a.salida:
        with open(a.salida, "w") as f:
            json.dump(out, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k in ("perp", "top50", "resumen", "universo"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--desde", default="2026-09-01"); ap.add_argument("--hasta", default="2026-10-01")
    ap.add_argument("--base", default="/tmp/servicio_simulado.db"); ap.add_argument("--salida")
    main(ap.parse_args())
