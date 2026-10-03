"""Comparación semanal papel ↔ motor (plan, fase 3): corre el motor de backtest sobre las mismas velas, universo y
volúmenes que tuvo el servicio, desde el mismo arranque y con la misma configuración, y lista las diferencias evento
por evento (entradas, salidas, cantidades, precios) y de patrimonio.

En el servidor (lee una copia de la base del servicio hecha con el backup de SQLite, sin tocarla):
    docker compose exec cascada python -m validacion.comparar_papel [--desde 2026-10-05] [--hasta …] [--sin_funding]

Sale con código 0 si no hay diferencias fuera de tolerancia; 1 si las hay (para correrlo programado).
"""
import argparse
import dataclasses
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from cascada import config as C
from cascada.db import Base

from .motor_bt import Corrida

H4 = pd.Timedelta(hours=4)
TOL_CONTRATOS = 0.02      # diferencia relativa de cantidad tolerada (redondeos de contrato)
TOL_PRECIO = 0.005        # 0,5 %: el papel llena al precio del momento, el motor a la apertura de la vela


def _ms(t):
    return int(pd.Timestamp(t).value // 10**6)


def leer_servidor(ruta):
    origen = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True)
    cx = sqlite3.connect(":memory:")
    origen.backup(cx); origen.close()                # copia consistente aunque el servicio esté escribiendo
    cx.row_factory = sqlite3.Row
    q = lambda sql, p=(): [dict(r) for r in cx.execute(sql, p).fetchall()]
    return cx, q


def cargador(cx, desde, hasta, funding):
    """Copia velas 4 h, universo y volúmenes del servidor en la base del motor y arma las matrices."""
    def cargar(db):
        v = pd.read_sql("SELECT simbolo, ts, o, h, l, c, v FROM velas WHERE tf='4h'", cx)
        v["t"] = pd.to_datetime(v.ts, unit="ms")
        v = v[v.t < hasta]
        bases = sorted(v.simbolo.unique())
        for b, d in v.groupby("simbolo"):
            db.guardar_velas(b, "4h", d.set_index("t")[["o", "h", "l", "c", "v"]].astype(float))
        M = {k: v.pivot(index="t", columns="simbolo", values=k).sort_index() for k in ("o", "h", "l", "c", "v")}
        with db._lock:
            db.cx.executemany("INSERT OR REPLACE INTO universo VALUES (?,?,?,?)",
                              [tuple(r) for r in cx.execute("SELECT fecha, simbolo, puesto, vol24 FROM universo").fetchall()])
            db.cx.executemany("INSERT OR REPLACE INTO vol_cmc VALUES (?,?,?)",
                              [tuple(r) for r in cx.execute("SELECT fecha, simbolo, vol FROM vol_cmc").fetchall()])
            db.cx.commit()
        return bases, M, funding
    return cargar


def funding_kucoin(simbolos, desde, hasta):
    """Funding liquidado de KuCoin por símbolo (lo que aplicó el papel). Necesita internet."""
    from cascada.bolsa import Publico
    pub = Publico(); filas = {}
    for b in simbolos:
        r = pub.funding_liquidado(b, _ms(desde) - 1) or []
        filas[b] = {pd.to_datetime(ts, unit="ms"): tasa for ts, tasa in r if ts <= _ms(hasta)}
    F = pd.DataFrame(filas).sort_index()
    return F if len(F) else None


def eventos(filas):
    d = pd.DataFrame(filas)
    if d.empty:
        return pd.DataFrame(columns=["t", "cuenta", "estrategia", "simbolo", "lado", "contratos", "precio", "motivo"])
    d["t"] = pd.to_datetime(d.ts, unit="ms").dt.floor("4h")
    return d[["t", "cuenta", "estrategia", "simbolo", "lado", "contratos", "precio", "motivo"]]


def emparejar(papel, motor):
    """Une por (vela, cuenta, estrategia, símbolo, lado) sumando cantidades; marca lo que no coincide."""
    clave = ["t", "cuenta", "estrategia", "simbolo", "lado"]
    agr = lambda d: d.groupby(clave).agg(contratos=("contratos", "sum"), precio=("precio", "mean"),
                                         motivo=("motivo", lambda x: ",".join(sorted(set(map(str, x)))))).reset_index()
    a, b = agr(papel), agr(motor)
    u = a.merge(b, on=clave, how="outer", suffixes=("_papel", "_motor"), indicator=True)
    # cantidad: tolera 1 contrato (el papel dimensiona con el precio del momento, el motor con la apertura) + 2 %
    dc = (u.contratos_papel - u.contratos_motor).abs()
    dif_c = np.where(dc <= 1.0 + 1e-9, 0.0, dc / u[["contratos_papel", "contratos_motor"]].max(axis=1))
    dif_p = (u.precio_papel / u.precio_motor - 1).abs()
    u["problema"] = np.select(
        [u._merge == "left_only", u._merge == "right_only", dif_c > TOL_CONTRATOS, dif_p > TOL_PRECIO],
        ["sólo en papel", "sólo en el motor", "cantidad distinta", "precio distinto"], default="")
    return u.drop(columns="_merge").sort_values(clave)


def main(a):
    cfg = C.cargar(a.config) if a.config else C.cargar()
    cx, q = leer_servidor(a.base or C.ruta_base(dataclasses.replace(cfg, modo="papel")))
    cfg.modo = "papel"
    pat = pd.DataFrame(q("SELECT ts, patrimonio FROM patrimonio WHERE cuenta='total' ORDER BY ts"))
    if pat.empty:
        raise SystemExit("La base no tiene patrimonio registrado: ¿el papel ya corrió algún ciclo?")
    pat["t"] = pd.to_datetime(pat.ts, unit="ms")
    ciclos = pat[pat.t == pat.t.dt.floor("4h")]
    desde = pd.Timestamp(a.desde) if a.desde else ciclos.t.iat[0]
    hasta = pd.Timestamp(a.hasta) if a.hasta else ciclos.t.iat[-1] + H4
    ops_p = q("SELECT * FROM operaciones WHERE ts>=? AND ts<?", (_ms(desde), _ms(hasta)))
    from cascada.bolsa import Publico
    merc = None
    if a.contratos:
        k = pd.read_csv(a.contratos).set_index("base")
        merc = {b: dict(tam=float(r.tam), minimo=float(r.minimo or 1), id=r.id) for b, r in k.iterrows() if r.tam == r.tam}
    else:
        merc = Publico().mercados()
    F = None
    if not a.sin_funding:
        F = funding_kucoin(sorted({o["simbolo"] for o in ops_p if o["cuenta"] == "principal"}), desde, hasta)
    r = Corrida(None, None, None, None, cfg.pesos, modo="PAPEL", corrida="B", capital=cfg.capital_papel, desde=desde,
                hasta=hasta, log_cada=0, cargar=cargador(cx, desde, hasta, F), mercados=merc, cfg=cfg).correr()
    ops_m = r.db.filas("SELECT * FROM operaciones WHERE ts>=? AND ts<?", (_ms(desde), _ms(hasta)))
    u = emparejar(eventos(ops_p), eventos(ops_m))
    mal = u[u.problema != ""]
    E_p = ciclos.set_index("t").patrimonio
    E_m = r.serie.E
    comunes = E_p.index.intersection(E_m.index)
    dif_E = (E_p.loc[comunes] / E_m.loc[comunes] - 1) if len(comunes) else pd.Series(dtype=float)
    print(f"Período {desde} → {hasta} · {len(ops_p)} operaciones en papel, {len(ops_m)} en el motor")
    print(f"Eventos: {len(u)} · coinciden {len(u) - len(mal)} · con diferencias {len(mal)}")
    if len(comunes):
        print(f"Patrimonio: papel {E_p.loc[comunes].iat[-1]:,.2f} · motor {E_m.loc[comunes].iat[-1]:,.2f} · "
              f"diferencia máxima {dif_E.abs().max():.2%}")
    if len(mal):
        print("\nDiferencias:")
        print(mal[["t", "cuenta", "estrategia", "simbolo", "lado", "contratos_papel", "contratos_motor", "precio_papel",
                   "precio_motor", "problema"]].to_string(index=False))
    if a.salida:
        out = dict(desde=str(desde), hasta=str(hasta), eventos=len(u), diferencias=len(mal),
                   patrimonio_dif_max=float(dif_E.abs().max()) if len(dif_E) else None,
                   detalle=json.loads(mal.to_json(orient="records", date_format="iso")))
        Path(a.salida).write_text(json.dumps(out, indent=1, ensure_ascii=False))
    sys.exit(1 if len(mal) else 0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="base del papel (por defecto datos/cascada_papel.db del servicio)")
    ap.add_argument("--desde"); ap.add_argument("--hasta"); ap.add_argument("--config")
    ap.add_argument("--contratos", help="csv de contratos (base,id,tam,minimo); por defecto los de KuCoin en vivo")
    ap.add_argument("--sin_funding", action="store_true"); ap.add_argument("--salida")
    main(ap.parse_args())
