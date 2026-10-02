"""Web del sistema: servidor HTTP de la biblioteca estándar, con usuario y clave (Basic Auth).
Detrás de Caddy (HTTPS). Sólo lectura, salvo pausar y reanudar."""
import base64
import hmac
import json
import logging
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

from . import estadisticas as ES

log = logging.getLogger("cascada.web")
INDEX = Path(__file__).resolve().parent / "estatico" / "index.html"
_cache = {}


def _cacheado(clave, seg, f):
    v = _cache.get(clave)
    if v and time.time() - v[0] < seg:
        return v[1]
    r = f(); _cache[clave] = (time.time(), r)
    return r


def _limpio(x):
    """NaN e infinitos a null (JSON válido para el navegador); numpy a tipos nativos."""
    if isinstance(x, dict):
        return {k: _limpio(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_limpio(v) for v in x]
    if hasattr(x, "item") and not isinstance(x, (str, bytes)):
        x = x.item()
    if isinstance(x, float) and (x != x or x in (float("inf"), float("-inf"))):
        return None
    return x


def _ts(ms):
    return pd.to_datetime(ms, unit="ms").strftime("%Y-%m-%d %H:%M")


def datos_todo(s):
    db, cfg = s.db, s.cfg
    inc = db.filas("SELECT * FROM incidencias ORDER BY id DESC LIMIT 100")
    for x in inc:
        x["fecha"] = _ts(x["ts"])
    return dict(kpis=ES.kpis(db, cfg), posiciones=ES.posiciones(db), estrategias=ES.por_estrategia(db),
                previsiones=db.get("previsiones") or [], incidencias=inc,
                rendimientos=ES.rendimientos(ES.valor_cuota(db, "total")), pesos=cfg.pesos, prioridad=cfg.prioridad,
                mercados=db.get("mercados_info") or {}, ahora=time.time())


def datos_patrimonio(s, rango):
    db = s.db
    desde = 0
    if rango != "todo":
        dias = int(rango.rstrip("d"))
        desde = int((pd.Timestamp.now("UTC").tz_localize(None) - pd.Timedelta(days=dias)).value // 10**6)
    out = {}
    for c in ("total", "principal", "balas"):
        x = ES.serie_patrimonio(db, c, desde)
        if len(x) > 1500:
            x = x.resample("4h" if len(x) > 6000 else "1h").last().dropna()
        out[c] = dict(t=[i.strftime("%Y-%m-%d %H:%M") for i in x.index], v=[round(v, 2) for v in x.values])
    q = ES.valor_cuota(db, "total")
    if len(q):
        q = q[q.index >= pd.to_datetime(desde, unit="ms")]
        if len(q) > 1500:
            q = q.resample("1h").last().dropna()
        dd = q / q.cummax() - 1
        out["caida"] = dict(t=[i.strftime("%Y-%m-%d %H:%M") for i in dd.index], v=[round(v, 4) for v in dd.values])
    return out


def datos_operaciones(s, limite, estrategia=None):
    sql = "SELECT * FROM operaciones" + (" WHERE estrategia=?" if estrategia else "") + " ORDER BY id DESC LIMIT ?"
    ops = s.db.filas(sql, ((estrategia,) if estrategia else ()) + (limite,))
    for o in ops:
        o["fecha"] = _ts(o["ts"])
    lotes = s.db.filas("SELECT * FROM lotes WHERE cerrado_ts IS NOT NULL" + (" AND estrategia=?" if estrategia else "")
                       + " ORDER BY cerrado_ts DESC LIMIT ?", ((estrategia,) if estrategia else ()) + (limite,))
    for L in lotes:
        L["abierto"] = _ts(L["abierto_ts"]); L["cerrado"] = _ts(L["cerrado_ts"])
        n = abs(L["contratos"]) * L["tam_contrato"] * L["precio_entrada"]
        L["pnl_pct"] = (L["pnl"] or 0) / n if n else None
    return dict(operaciones=ops, lotes=lotes)


def datos_comparacion(s):
    db, cfg = s.db, s.cfg
    k = ES.kpis(db, cfg)
    return dict(comparacion=ES.comparacion_vivo(db, cfg), banda=ES.banda_esperada(cfg, k.get("dias", 0)), nivel=cfg.nivel)


class Manejador(BaseHTTPRequestHandler):
    sistema = None
    server_version = "cascada"

    def log_message(self, fmt, *args):
        pass

    def _autorizado(self):
        cfg = self.sistema.cfg
        if not cfg.web_clave:
            return False
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            u, _, p = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:
            return False
        return hmac.compare_digest(u, cfg.web_usuario) and hmac.compare_digest(p, cfg.web_clave)

    def _enviar(self, codigo, cuerpo, tipo="application/json; charset=utf-8"):
        if not isinstance(cuerpo, bytes):
            cuerpo = json.dumps(_limpio(cuerpo), default=str).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _negar(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="cascada"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        s = self.sistema
        if u.path == "/salud":
            d = salud(s)
            return self._enviar(200 if d["ok"] else 503, d)
        if not s.cfg.web_clave:
            return self._enviar(503, b"Falta WEB_CLAVE en .env", "text/plain; charset=utf-8")
        if not self._autorizado():
            return self._negar()
        try:
            if u.path in ("/", "/index.html"):
                return self._enviar(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/estatico/chart.umd.js":
                return self._enviar(200, (INDEX.parent / "chart.umd.js").read_bytes(), "application/javascript; charset=utf-8")
            if u.path == "/api/todo":
                return self._enviar(200, datos_todo(s))
            if u.path == "/api/patrimonio":
                r = q.get("rango", ["30d"])[0]
                return self._enviar(200, _cacheado(("pat", r), 30, lambda: datos_patrimonio(s, r)))
            if u.path == "/api/operaciones":
                lim = min(int(q.get("limite", ["300"])[0]), 5000)
                return self._enviar(200, datos_operaciones(s, lim, q.get("estrategia", [None])[0]))
            if u.path == "/api/backtest":
                return self._enviar(200, _cacheado("bt", 3600, ES.backtest))
            if u.path == "/api/comparacion":
                return self._enviar(200, _cacheado("cmp", 300, lambda: datos_comparacion(s)))
            return self._enviar(404, dict(error="no existe"))
        except Exception as e:
            log.exception("web %s", u.path)
            return self._enviar(500, dict(error=str(e)))

    def do_POST(self):
        s = self.sistema
        if not s.cfg.web_clave or not self._autorizado():
            return self._negar()
        if urlparse(self.path).path != "/api/comando":
            return self._enviar(404, dict(error="no existe"))
        largo = min(int(self.headers.get("Content-Length", 0)), 10_000)
        try:
            nombre = json.loads(self.rfile.read(largo) or b"{}").get("nombre")
        except Exception:
            nombre = None
        if nombre not in ("pausar", "reanudar"):
            return self._enviar(400, dict(error="sólo pausar o reanudar desde la web"))
        r = queue.Queue()
        s.cola_web.put((nombre, [], r))
        try:
            return self._enviar(200, dict(respuesta=r.get(timeout=30)))
        except queue.Empty:
            return self._enviar(504, dict(error="el motor está ocupado, probá en un minuto"))


def salud(s):
    """Salud real del servicio: bucle vivo, control de riesgo vivo, ciclo al día, sin incidencias críticas,
    libro conciliado y sin cierre total pendiente. La usa el HEALTHCHECK de Docker."""
    db = s.db; ahora = time.time()
    inicio = db.get("inicio") or ahora
    lat = db.get("latido") or 0
    latc = db.get("latido_control") or inicio
    uc = (db.get("ultimo_ciclo") or {}).get("ts") or inicio
    criticas = db.filas("SELECT mensaje FROM incidencias WHERE nivel='critica' AND resuelta=0 ORDER BY id DESC LIMIT 3")
    fallas = []
    if ahora - lat > 120:
        fallas.append(f"bucle principal sin latido hace {ahora - lat:.0f} s")
    if ahora - latc > 20 * 60:
        fallas.append(f"control de riesgo sin correr hace {(ahora - latc) / 60:.0f} min")
    if ahora - uc > 4 * 3600 + 20 * 60:
        fallas.append(f"último ciclo hace {(ahora - uc) / 3600:.1f} h")
    if criticas:
        fallas.append("incidencias críticas: " + " | ".join(c["mensaje"][:120] for c in criticas))
    if not db.get("conciliacion_ok", True):
        fallas.append("libro y exchange no coinciden")
    if db.get("liquidando"):
        fallas.append("cierre total pendiente")
    return dict(ok=not fallas, fallas=fallas, latido_seg=round(ahora - lat), version=db.get("version"))


def arrancar(sistema, puerto=8080, host="0.0.0.0"):
    Manejador.sistema = sistema
    srv = ThreadingHTTPServer((host, puerto), Manejador)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("web en %s:%s", host, puerto)
    return srv
