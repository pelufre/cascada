"""Paquete de datos de la validación: lectura con huellas SHA-256 y corte forzado del período de prueba.

Formato (el que arma `notebooks/descargar_perpetuos_4h.ipynb`): matrices con índice = apertura de la vela (UTC) y una
columna por símbolo: `_aperturas_4h_perp.csv`, `_maximos_…`, `_minimos_…`, `_cierres_…`, `_volumen_usdt_…`,
`_funding_perp.csv`, `listados_perp.csv`, `contratos_kucoin.csv` y `MANIFIESTO.json`.
También lee las matrices viejas de spot (sin sufijo y sin funding) para desarrollar el motor; esas corridas no cuentan.
"""
import hashlib
import json
import os
from pathlib import Path

import pandas as pd

INICIO_IS = pd.Timestamp("2019-01-01")
CORTE_IS = pd.Timestamp("2024-01-01")          # exclusivo: IS = [2019-01-01, 2024-01-01)
FIN_OOS = pd.Timestamp("2026-10-01")
RAIZ = Path(__file__).resolve().parent
CONGELADO = RAIZ / "pesos_congelados.json"


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def autorizado_oos():
    """El período de prueba sólo se abre con los pesos congelados en un commit (protocolo §5.7 y §6)."""
    if os.environ.get("CASCADA_ABRIR_OOS") != "1" or not CONGELADO.exists():
        return False
    import subprocess
    r = subprocess.run(["git", "log", "--format=%H", "--", str(CONGELADO)], cwd=RAIZ, capture_output=True, text=True)
    sucio = subprocess.run(["git", "status", "--porcelain", "--", str(CONGELADO)], cwd=RAIZ, capture_output=True, text=True)
    return bool(r.stdout.strip()) and not sucio.stdout.strip()


def limite(modo):
    if modo == "IS":
        return CORTE_IS
    if modo == "OOS":
        if not autorizado_oos():
            raise PermissionError("El período 2024–2026 está cerrado: primero hay que congelar los pesos (protocolo §5.7)")
        return FIN_OOS
    raise ValueError(modo)


class Paquete:
    def __init__(self, carpeta, verificar=True):
        self.dir = Path(carpeta)
        self.perp = (self.dir / "_cierres_4h_perp.csv").exists()
        self.suf = "_4h_perp" if self.perp else "_4h"
        man = self.dir / "MANIFIESTO.json"
        self.manifiesto = json.loads(man.read_text()) if man.exists() else None
        if verificar and self.manifiesto:
            malos = [n for n, h in self.manifiesto["archivos"].items() if (self.dir / n).exists() and _sha(self.dir / n) != h]
            if malos:
                raise ValueError(f"Huella SHA-256 distinta en: {malos}")

    def _leer(self, nombre, hasta):
        d = pd.read_csv(self.dir / nombre, index_col=0)
        d.index = pd.to_datetime(d.index, utc=True).tz_localize(None)
        return d[d.index < hasta]

    def matrices(self, hasta):
        """Velas con apertura < hasta (la última vela cerrada antes del corte)."""
        hasta = pd.Timestamp(hasta)
        hasta_vela = hasta - pd.Timedelta(hours=4)       # la vela que abre a las 20:00 cierra en el corte
        out = {}
        for k, n in (("o", "_aperturas"), ("h", "_maximos"), ("l", "_minimos"), ("c", "_cierres"), ("v", "_volumen_usdt")):
            out[k] = self._leer(f"{n}{self.suf}.csv", hasta_vela + pd.Timedelta(seconds=1))
        return out

    def funding(self, hasta):
        p = self.dir / "_funding_perp.csv"
        return self._leer("_funding_perp.csv", pd.Timestamp(hasta)) if p.exists() else None

    def contratos(self):
        p = self.dir / "contratos_kucoin.csv"
        return pd.read_csv(p).set_index("base") if p.exists() else None


def cargar_base(db, paquete, top50, resumen, universo, hasta, desde="2018-06-01"):
    """Carga velas, universo semanal (top 50 ∩ símbolos con perpetuo listado esa semana) y volumen diario en la base."""
    M = paquete.matrices(hasta)
    sel = M["c"].index >= pd.Timestamp(desde)
    bases = []
    for col in M["c"].columns:
        d = pd.DataFrame({k: M[k][col].values[sel] for k in M}, index=M["c"].index[sel]).dropna(subset=["c"])
        if not len(d):
            continue
        d["v"] = d["v"].fillna(0)
        for k in ("o", "h", "l"):
            d[k] = d[k].fillna(d["c"])
        db.guardar_velas(col, "4h", d)
        bases.append(col)
    top = pd.read_csv(top50, parse_dates=["fecha"])
    res = pd.read_csv(resumen); uni = pd.read_csv(universo)
    slug2col = dict(zip(res.slug, res.symbol)); id2slug = dict(zip(uni.cmc_id, uni.slug))
    top = top[(top.slug != "uni-coin") & (top.fecha < pd.Timestamp(hasta))].copy()
    top["col"] = top.cmc_id.map(id2slug).map(slug2col)
    top = top[top.col.isin(bases)]
    C = M["c"]
    filas = []
    for r in top.itertuples():
        f = r.fecha
        ventana = C.loc[(C.index > f - pd.Timedelta(days=7)) & (C.index <= f + pd.Timedelta(hours=20)), r.col]
        if ventana.notna().any():               # el perpetuo ya estaba listado esa semana
            filas.append((f.strftime("%Y-%m-%d"), r.col, int(r.puesto), 0.0))
    with db._lock:
        db.cx.executemany("INSERT OR REPLACE INTO universo VALUES (?,?,?,?)", filas)
        vd = M["v"][sel].groupby(M["v"].index[sel].floor("D")).sum(min_count=1)
        db.cx.executemany("INSERT OR REPLACE INTO vol_cmc VALUES (?,?,?)",
                          [(d.strftime("%Y-%m-%d"), s, float(v)) for d, row in vd.iterrows() for s, v in row.items() if v == v])
        db.cx.commit()
    return bases, M
