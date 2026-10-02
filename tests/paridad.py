"""Paridad: corre el motor en modo papel vela por vela sobre datos históricos y compara con el backtest.

Uso (en la máquina de investigación, con los CSV de velas 4h del top 50):
    python -m tests.paridad --velas DIR --top50 top50_semanal.csv --resumen resumen_descarga.csv --universo universo.csv
"""
import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd

from cascada import config as C
from cascada.bolsa import Papel
from cascada.datos import Datos
from cascada.db import Base
from cascada.motor import Motor


class PublicoFalso:
    def __init__(self, db, bases):
        self.db = db
        self.m = {b: dict(tam=1.0, minimo=1e-6, id=b) for b in bases}
        self.t = None

    def mercados(self, refrescar=False):
        return self.m

    def precios(self, bases):
        out = {}
        for b in bases:
            r = self.db.filas("SELECT c FROM velas WHERE simbolo=? AND tf='4h' AND ts < ? ORDER BY ts DESC LIMIT 1",
                              (b, int(self.t.value // 10**6) - 4 * 3600_000 + 1))
            if r:
                out[b] = r[0]["c"]
        return out


def cargar_datos(db, carpeta, top50, resumen, universo, desde="2023-06-01"):
    C_ = pd.read_csv(Path(carpeta) / "_cierres_4h.csv", index_col=0)
    idx = pd.to_datetime(C_.index, utc=True).tz_localize(None)
    sel = idx >= pd.Timestamp(desde)
    tablas = {k: pd.read_csv(Path(carpeta) / f, index_col=0)[sel] for k, f in
              (("o", "_aperturas_4h.csv"), ("h", "_maximos_4h.csv"), ("l", "_minimos_4h.csv"), ("c", "_cierres_4h.csv"),
               ("v", "_volumen_usdt_4h.csv"))}
    ix = idx[sel]
    bases = []
    for col in tablas["c"].columns:
        d = pd.DataFrame({k: tablas[k][col].values for k in tablas}, index=ix).dropna(subset=["c"])
        d["v"] = d["v"].fillna(0)
        for k in ("o", "h", "l"):
            d[k] = d[k].fillna(d["c"])
        if len(d):
            db.guardar_velas(col, "4h", d); bases.append(col)
    top = pd.read_csv(top50, parse_dates=["fecha"])
    res = pd.read_csv(resumen); uni = pd.read_csv(universo)
    slug2col = dict(zip(res.slug, res.symbol)); id2slug = dict(zip(uni.cmc_id, uni.slug))
    top = top[top.slug != "uni-coin"].copy()
    top["col"] = top.cmc_id.map(id2slug).map(slug2col)
    top = top[top.col.isin(bases)]
    for r in top.itertuples():
        db.ejec("INSERT OR REPLACE INTO universo VALUES (?,?,?,?)", (r.fecha.strftime("%Y-%m-%d"), r.col, int(r.puesto), 0.0))
    # volumen diario en USD (el mismo del backtest) para el filtro de liquidez de c40
    vd = tablas["v"].groupby(ix.floor("D")).sum(min_count=1)
    filas = [(d.strftime("%Y-%m-%d"), s, float(v)) for d, row in vd.iterrows() for s, v in row.items() if v == v]
    with db._lock:
        db.cx.executemany("INSERT OR REPLACE INTO vol_cmc VALUES (?,?,?)", filas); db.cx.commit()
    return bases


def correr(args):
    ruta = Path(args.base); ruta.unlink(missing_ok=True)
    db = Base(ruta)
    t0 = time.time()
    bases = cargar_datos(db, args.velas, args.top50, args.resumen, args.universo)
    print(f"datos cargados: {len(bases)} monedas en {time.time() - t0:.0f} s")
    cfg = C.Config(nivel=args.nivel, pesos=dict(C.NIVELES[args.nivel]), capital_papel=args.capital, corte_caida=9, alerta_caida=9)
    pub = PublicoFalso(db, bases)
    datos = Datos(db, pub)
    bolsa = Papel(db, pub.m, capital=args.capital, desliz=0.0, comision=0.0011)
    motor = Motor(cfg, db, datos, bolsa, pub)
    ts = pd.date_range(args.desde, args.hasta, freq="4h")
    t0 = time.time()
    for k, t in enumerate(ts):
        pub.t = t
        lib = motor.libro()
        bs = {L["simbolo"] for L in lib.values()} | {"BTC", "ETH"}
        velas = {}
        for b in bs:
            d = datos.v4(b, t, dias=1)
            if len(d):
                velas[b] = (d.o.iat[-1], d.h.iat[-1], d.l.iat[-1])
        motor.ciclo(t, velas_cerradas=velas)
        if k % 500 == 0:
            print(t, f"{time.time() - t0:.0f} s", round(bolsa.patrimonio(), 2))
    # en mercado por estrategia y día
    ops = pd.DataFrame(db.filas("SELECT estrategia, abierto_ts, cerrado_ts FROM lotes"))
    dias = pd.date_range(args.desde, args.hasta, freq="D")
    act = pd.DataFrame(0, index=dias, columns=sorted(ops.estrategia.unique()))
    for r in ops.itertuples():
        a = pd.to_datetime(r.abierto_ts, unit="ms"); b = pd.to_datetime(r.cerrado_ts, unit="ms") if pd.notna(r.cerrado_ts) else dias[-1]
        act.loc[a.floor("D"):b.floor("D"), r.estrategia] = 1
    act.to_csv(args.salida)
    eq = pd.DataFrame(db.filas("SELECT ts, patrimonio FROM patrimonio WHERE cuenta='principal'"))
    eq.to_csv(Path(args.salida).with_name("paridad_patrimonio.csv"), index=False)
    print("listo", f"{time.time() - t0:.0f} s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--velas"); ap.add_argument("--top50"); ap.add_argument("--resumen"); ap.add_argument("--universo")
    ap.add_argument("--desde", default="2025-01-01"); ap.add_argument("--hasta", default="2026-09-30")
    ap.add_argument("--nivel", default="techo_intrabarra_20"); ap.add_argument("--capital", type=float, default=100000)
    ap.add_argument("--base", default="/tmp/paridad.db"); ap.add_argument("--salida", default="/tmp/paridad_actividad.csv")
    correr(ap.parse_args())
