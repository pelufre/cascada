"""Web del Sistema 3 (servidor HTTP de la biblioteca estándar, Basic Auth, detrás de Caddy con HTTPS).
Sólo lectura, salvo confirmar o descartar la decisión pendiente y pausar o reanudar."""
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
from .senales import NIVELES

log = logging.getLogger("sistema3.web")
ESTATICO = Path(__file__).resolve().parent / "estatico"
_cache = {}


def _cacheado(clave, seg, f):
    v = _cache.get(clave)
    if v and time.time() - v[0] < seg:
        return v[1]
    r = f(); _cache[clave] = (time.time(), r)
    return r


def _limpio(x):
    if isinstance(x, dict):
        return {k: _limpio(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_limpio(v) for v in x]
    if hasattr(x, "item") and not isinstance(x, (str, bytes)):
        x = x.item()
    if isinstance(x, float) and (x != x or x in (float("inf"), float("-inf"))):
        return None
    return x


def datos_todo(s):
    db, m = s.db, s.motor
    try:
        v = m.valuar()
        pos = ES.posiciones(m, v)
    except Exception as e:
        v, pos = dict(error=str(e)), []
    inc = db.filas("SELECT * FROM incidencias ORDER BY id DESC LIMIT 80")
    for x in inc:
        x["fecha"] = ES.ts(x["ts"])
    dec = db.filas("SELECT id, ts, vela, tipo, estado, resumen, vence_ts, resultado FROM decisiones ORDER BY ts DESC LIMIT 40")
    for x in dec:
        x["fecha"] = ES.ts(x["ts"]); x["vence"] = ES.ts(x["vence_ts"])
    modos = db.filas("SELECT * FROM modos ORDER BY fecha DESC LIMIT 60")
    L = m.libro
    return dict(kpis=ES.kpis(db), valuacion=v, posiciones=pos, incidencias=inc, decisiones=dec, modos=modos,
                rendimientos=ES.rendimientos(ES.cuota(db)), cerradas=ES.resumen_cerradas(db),
                estado=dict(modo=db.get("modo_actual"), conflicto=L["conflicto"], pausado=bool(db.get("pausado")),
                            cuenta=s.cfg.modo, nivel=s.cfg.nivel, parametros=NIVELES[s.cfg.nivel], confirmar=s.cfg.confirmar,
                            ultimo_ciclo=db.get("ultimo_ciclo"), version=db.get("version")),
                ahora=time.time())


def datos_patrimonio(s, rango):
    desde = 0
    if rango != "todo":
        desde = int((pd.Timestamp.now("UTC").tz_localize(None) - pd.Timedelta(days=int(rango.rstrip("d")))).value // 10**6)
    d = ES.serie(s.db, desde)
    q = ES.cuota(s.db)
    q = q[q.index >= pd.to_datetime(desde, unit="ms")] if len(q) else q
    if len(d) > 1500:
        d = d.resample("1h").last().dropna(); q = q.resample("1h").last().dropna()
    dd = q / q.cummax() - 1 if len(q) else q
    f = lambda idx: [i.strftime("%Y-%m-%d %H:%M") for i in idx]
    return dict(t=f(d.index), total=[round(x, 2) for x in d["total"]], btc=[x for x in d["btc"]],
                largos=[round(x, 3) for x in d["expo_largos"]], cortos=[round(x, 3) for x in d["expo_cortos"]],
                caida=dict(t=f(dd.index), v=[round(x, 4) for x in dd]))


def datos_operaciones(s, limite):
    ops = s.db.filas("SELECT * FROM operaciones ORDER BY id DESC LIMIT ?", (limite,))
    for o in ops:
        o["fecha"] = ES.ts(o["ts"])
    cer = s.db.filas("SELECT * FROM cerradas ORDER BY id DESC LIMIT ?", (limite,))
    for c in cer:
        c["abierto"] = ES.ts(c["abierto_ts"]); c["cerrado"] = ES.ts(c["cerrado_ts"])
    return dict(operaciones=ops, cerradas=cer)


class Manejador(BaseHTTPRequestHandler):
    servicio = None
    server_version = "sistema3"

    def log_message(self, fmt, *args):
        pass

    def _autorizado(self):
        cfg = self.servicio.cfg
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
        self.send_header("WWW-Authenticate", 'Basic realm="sistema3"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        s = self.servicio
        if u.path == "/salud":
            d = salud(s)
            return self._enviar(200 if d["ok"] else 503, d)
        if not s.cfg.web_clave:
            return self._enviar(503, b"Falta WEB_CLAVE en .env", "text/plain; charset=utf-8")
        if not self._autorizado():
            return self._negar()
        try:
            if u.path in ("/", "/index.html"):
                return self._enviar(200, (ESTATICO / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/estatico/chart.umd.js":
                return self._enviar(200, (ESTATICO / "chart.umd.js").read_bytes(), "application/javascript; charset=utf-8")
            if u.path == "/api/todo":
                return self._enviar(200, _cacheado("todo", 20, lambda: datos_todo(s)))
            if u.path == "/api/patrimonio":
                r = q.get("rango", ["30d"])[0]
                return self._enviar(200, _cacheado(("pat", r), 30, lambda: datos_patrimonio(s, r)))
            if u.path == "/api/operaciones":
                return self._enviar(200, datos_operaciones(s, min(int(q.get("limite", ["300"])[0]), 5000)))
            return self._enviar(404, dict(error="no existe"))
        except Exception as e:
            log.exception("web %s", u.path)
            return self._enviar(500, dict(error=str(e)))

    def do_POST(self):
        s = self.servicio
        if not s.cfg.web_clave or not self._autorizado():
            return self._negar()
        if urlparse(self.path).path != "/api/comando":
            return self._enviar(404, dict(error="no existe"))
        largo = min(int(self.headers.get("Content-Length", 0)), 10_000)
        try:
            d = json.loads(self.rfile.read(largo) or b"{}")
        except Exception:
            d = {}
        nombre = d.get("nombre")
        if nombre not in ("si", "no", "pausar", "reanudar"):
            return self._enviar(400, dict(error="comando no permitido"))
        r = queue.Queue()
        s.cola_web.put((nombre, [str(d.get("id", ""))] if d.get("id") else [], r))
        _cache.pop("todo", None)
        try:
            return self._enviar(200, dict(respuesta=r.get(timeout=30)))
        except queue.Empty:
            return self._enviar(504, dict(error="el sistema está ocupado, probá en un minuto"))


def salud(s):
    db = s.db; ahora = time.time()
    inicio = db.get("inicio") or ahora
    lat = db.get("latido") or 0
    uc = (db.get("ultimo_ciclo") or {}).get("ts") or inicio
    fallas = []
    if ahora - lat > 180:
        fallas.append(f"bucle sin latido hace {ahora - lat:.0f} s")
    if ahora - uc > 4 * 3600 + 30 * 60:
        fallas.append(f"último ciclo hace {(ahora - uc) / 3600:.1f} h")
    crit = db.filas("SELECT mensaje FROM incidencias WHERE nivel='critica' AND resuelta=0 ORDER BY id DESC LIMIT 3")
    if crit:
        fallas.append("incidencias críticas: " + " | ".join(c["mensaje"][:120] for c in crit))
    return dict(ok=not fallas, fallas=fallas, version=db.get("version"))


def arrancar(servicio, puerto=8080, host="0.0.0.0"):
    Manejador.servicio = servicio
    srv = ThreadingHTTPServer((host, puerto), Manejador)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("web en %s:%s", host, puerto)
    return srv
