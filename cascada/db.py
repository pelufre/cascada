"""Base SQLite: velas, estado, libro de lotes por estrategia, operaciones, patrimonio e incidencias.

`transaccion()` agrupa varias escrituras: quedan todas o ninguna (lo usa el motor para aplicar una orden al libro, de
modo que una caída del proceso a mitad de camino no deja una parte de la asignación escrita)."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

ESQUEMA = """
CREATE TABLE IF NOT EXISTS velas (simbolo TEXT, tf TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
    PRIMARY KEY (simbolo, tf, ts));
CREATE TABLE IF NOT EXISTS estado (clave TEXT PRIMARY KEY, valor TEXT);
CREATE TABLE IF NOT EXISTS lotes (id TEXT PRIMARY KEY, cuenta TEXT, estrategia TEXT, simbolo TEXT, lado INTEGER,
    contratos REAL, tam_contrato REAL, precio_entrada REAL, stop REAL, stop_orden TEXT, reescalable INTEGER,
    abierto_ts INTEGER, cerrado_ts INTEGER, precio_salida REAL, motivo_salida TEXT, pnl REAL, meta TEXT);
CREATE TABLE IF NOT EXISTS operaciones (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, cuenta TEXT, estrategia TEXT,
    lote TEXT, simbolo TEXT, lado TEXT, contratos REAL, precio REAL, nocional REAL, comision REAL, motivo TEXT,
    modo TEXT, client_oid TEXT, orden_id TEXT);
CREATE TABLE IF NOT EXISTS patrimonio (ts INTEGER, cuenta TEXT, patrimonio REAL, nocional REAL, maximo REAL, caida REAL,
    PRIMARY KEY (ts, cuenta));
CREATE TABLE IF NOT EXISTS asignacion (ts INTEGER, estrategia TEXT, pedido REAL, concedido REAL, nocional REAL,
    PRIMARY KEY (ts, estrategia));
CREATE TABLE IF NOT EXISTS incidencias (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, nivel TEXT, tipo TEXT,
    mensaje TEXT, resuelta INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS universo (fecha TEXT, simbolo TEXT, puesto INTEGER, vol24 REAL, PRIMARY KEY (fecha, simbolo));
CREATE TABLE IF NOT EXISTS vol_cmc (fecha TEXT, simbolo TEXT, vol REAL, PRIMARY KEY (fecha, simbolo));
CREATE TABLE IF NOT EXISTS flujos (ts INTEGER, cuenta TEXT, monto REAL, nota TEXT);
CREATE TABLE IF NOT EXISTS ordenes (client_oid TEXT PRIMARY KEY, ts INTEGER, simbolo TEXT, lado TEXT, contratos REAL,
    reduce INTEGER, estado TEXT, orden_id TEXT, llenado REAL, precio REAL, comision REAL, plan TEXT,
    aplicado INTEGER DEFAULT 0, intentos INTEGER DEFAULT 0, error TEXT);
CREATE INDEX IF NOT EXISTS ix_ops_ts ON operaciones(ts);
CREATE INDEX IF NOT EXISTS ix_lotes_est ON lotes(estrategia, cerrado_ts);
CREATE TABLE IF NOT EXISTS transferencias (id TEXT PRIMARY KEY, ts INTEGER, accion TEXT, monto REAL, estado TEXT,
    ruta TEXT, sub_antes REAL, sub_despues REAL, pasos TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS balas_acciones (id TEXT PRIMARY KEY, ts INTEGER, vela TEXT, tipo TEXT, estado TEXT,
    pedido TEXT, resultado TEXT, error TEXT);
"""


def _json(o):
    """numpy y pandas a tipos nativos para guardar en JSON."""
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, pd.Timestamp):
        return str(o)
    raise TypeError(type(o).__name__)


class Base:
    def __init__(self, ruta):
        Path(ruta).parent.mkdir(parents=True, exist_ok=True)
        self.ruta = str(ruta)
        self._lock = threading.RLock()
        self.cx = sqlite3.connect(self.ruta, check_same_thread=False, timeout=30)
        self.cx.row_factory = sqlite3.Row
        self.cx.execute("PRAGMA journal_mode=WAL")
        self.cx.executescript(ESQUEMA)
        for tabla, nuevas in (("lotes", (("comision", "REAL"), ("stop_contratos", "REAL"))),
                              # lo ya aplicado al libro de una orden que llenó en partes (NULL = nada aplicado todavía)
                              ("ordenes", (("llenado_aplicado", "REAL"), ("precio_aplicado", "REAL"),
                                           ("comision_aplicada", "REAL"), ("enviada_ms", "INTEGER"),
                                           ("precio_ref", "REAL")))):
            cols = {r[1] for r in self.cx.execute(f"PRAGMA table_info({tabla})")}
            for col, tipo in nuevas:
                if col not in cols:
                    self.cx.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}")
        self.cx.commit()
        self._tx = 0          # profundidad de la transacción abierta (la tiene el hilo que tiene el cerrojo)

    def ejec(self, sql, args=()):
        with self._lock:
            cur = self.cx.execute(sql, args)
            if not self._tx:
                self.cx.commit()
            return cur

    @contextmanager
    def transaccion(self):
        """Todo lo que se escribe adentro queda junto (commit al salir) o no queda (rollback ante cualquier excepción,
        incluida la interrupción del proceso). Se puede anidar: manda la más externa."""
        with self._lock:
            self._tx += 1
            try:
                yield self
            except BaseException:
                self._tx -= 1
                if not self._tx:
                    self.cx.rollback()
                raise
            self._tx -= 1
            if not self._tx:
                self.cx.commit()

    def filas(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self.cx.execute(sql, args).fetchall()]

    # --- estado clave/valor ---
    def get(self, clave, defecto=None):
        r = self.filas("SELECT valor FROM estado WHERE clave=?", (clave,))
        return json.loads(r[0]["valor"]) if r else defecto

    def set(self, clave, valor):
        self.ejec("INSERT OR REPLACE INTO estado VALUES (?,?)", (clave, json.dumps(valor, default=_json)))

    # --- velas ---
    def guardar_velas(self, simbolo, tf, df):
        if df is None or df.empty:
            return
        filas = [(simbolo, tf, int(ts), float(r.o), float(r.h), float(r.l), float(r.c), float(r.v))
                 for ts, r in zip(pd.DatetimeIndex(df.index).values.astype("datetime64[ms]").astype("int64"), df.itertuples())]
        with self._lock:
            self.cx.executemany("INSERT OR REPLACE INTO velas VALUES (?,?,?,?,?,?,?,?)", filas)
            if not self._tx:
                self.cx.commit()

    def velas(self, simbolo, tf, desde_ms=0):
        with self._lock:
            d = pd.read_sql_query("SELECT ts,o,h,l,c,v FROM velas WHERE simbolo=? AND tf=? AND ts>=? ORDER BY ts",
                                  self.cx, params=(simbolo, tf, desde_ms))
        d.index = pd.to_datetime(d.pop("ts"), unit="ms")
        return d

    def ultimo_ts(self, simbolo, tf):
        r = self.filas("SELECT MAX(ts) m FROM velas WHERE simbolo=? AND tf=?", (simbolo, tf))
        return r[0]["m"] if r and r[0]["m"] is not None else None

    # --- incidencias ---
    def incidencia(self, nivel, tipo, mensaje, dedupe_horas=6):
        """Registra una incidencia; si la misma (tipo+mensaje) sigue abierta y es reciente, no la duplica."""
        ahora = int(time.time() * 1000)
        r = self.filas("SELECT id, ts FROM incidencias WHERE tipo=? AND mensaje=? AND resuelta=0 ORDER BY id DESC LIMIT 1",
                       (tipo, mensaje))
        if r and ahora - r[0]["ts"] < dedupe_horas * 3600e3:
            return None
        cur = self.ejec("INSERT INTO incidencias (ts,nivel,tipo,mensaje) VALUES (?,?,?,?)", (ahora, nivel, tipo, mensaje))
        return cur.lastrowid

    def resolver(self, tipo):
        self.ejec("UPDATE incidencias SET resuelta=1 WHERE tipo=? AND resuelta=0", (tipo,))
