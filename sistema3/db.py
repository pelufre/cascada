"""Base SQLite del Sistema 3: estado, operaciones, operaciones cerradas, patrimonio, incidencias, universo, modos y decisiones.

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
CREATE TABLE IF NOT EXISTS estado (clave TEXT PRIMARY KEY, valor TEXT);
CREATE TABLE IF NOT EXISTS operaciones (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, parte TEXT, simbolo TEXT,
    lado TEXT, cantidad REAL, precio REAL, usdt REAL, comision REAL, motivo TEXT, modo TEXT, orden TEXT);
CREATE TABLE IF NOT EXISTS cerradas (id INTEGER PRIMARY KEY AUTOINCREMENT, parte TEXT, simbolo TEXT, lado TEXT,
    abierto_ts INTEGER, cerrado_ts INTEGER, entrada REAL, salida REAL, invertido REAL, pnl REAL, pnl_pct REAL, motivo TEXT);
CREATE TABLE IF NOT EXISTS patrimonio (ts INTEGER PRIMARY KEY, total REAL, spot REAL, futuros REAL, btc REAL,
    expo_largos REAL, expo_cortos REAL);
CREATE TABLE IF NOT EXISTS flujos (ts INTEGER, monto REAL, nota TEXT);
CREATE TABLE IF NOT EXISTS incidencias (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, nivel TEXT, tipo TEXT,
    mensaje TEXT, resuelta INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS universo (fecha TEXT, simbolo TEXT, puesto INTEGER, PRIMARY KEY (fecha, simbolo));
CREATE TABLE IF NOT EXISTS modos (fecha TEXT PRIMARY KEY, modo TEXT, btc_ok INTEGER, amplitud REAL, amp_on INTEGER,
    btc REAL, sma140 REAL, roc84 REAL);
CREATE TABLE IF NOT EXISTS decisiones (id TEXT PRIMARY KEY, ts INTEGER, vela TEXT, tipo TEXT, estado TEXT, resumen TEXT,
    datos TEXT, vence_ts INTEGER, resultado TEXT);
CREATE INDEX IF NOT EXISTS ix_ops_ts ON operaciones(ts);
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
