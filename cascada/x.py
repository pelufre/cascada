"""Publicación en X (Twitter): cada ciclo con operaciones genera un post, más un resumen diario.

API v2 (POST /2/tweets) con OAuth 1.0a de usuario, firmada a mano (sin librerías externas).
X cobra por post (sin plan gratis desde 2026; los posts con enlaces cuestan mucho más), por eso:
un solo post por ciclo agrupando todas las operaciones, sin enlaces, y un tope diario.
En modo papel todos los posts dicen SIMULACIÓN.
"""
import base64
import hashlib
import hmac
import json
import logging
import queue
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

log = logging.getLogger("cascada.x")
URL_POST = "https://api.x.com/2/tweets"
URL_YO = "https://api.x.com/2/users/me"

NOMBRES = {"ab_cortos": "Cortos Aberration", "mom_alts": "Momentum alts", "balas5": "30 balas BTC", "rsi2_btc": "RSI(2) BTC",
           "wr2": "WR2 BTC", "sold_btc": "Soldados BTC", "rsi2_eth": "RSI(2) ETH"}


def _enc(s):
    return urllib.parse.quote(str(s), safe="~")


def _pct(x, dec=1):
    if x is None:
        return "—"
    if abs(x) < 0.5 * 10 ** -(dec + 2):
        return f"0,{'0' * dec}%"
    s = f"{x * 100:+.{dec}f}%".replace(".", ",")
    return s.replace("-", "−")


class X:
    def __init__(self, cred, avisar=None, max_dia=12):
        self.cred = cred
        self.activo = all(cred.get(k) for k in ("api_key", "api_secret", "access_token", "access_secret"))
        self.avisar = avisar or (lambda n, t: None)
        self.max_dia = max_dia
        self._cola = queue.Queue()
        self._hoy = (None, 0)
        if self.activo:
            threading.Thread(target=self._enviador, daemon=True).start()

    # ---------- OAuth 1.0a
    def _cabecera(self, metodo, url, params=None):
        o = {"oauth_consumer_key": self.cred["api_key"], "oauth_nonce": secrets.token_hex(16),
             "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": str(int(time.time())),
             "oauth_token": self.cred["access_token"], "oauth_version": "1.0"}
        todos = {**o, **(params or {})}
        base = "&".join([metodo.upper(), _enc(url), _enc("&".join(f"{_enc(k)}={_enc(v)}" for k, v in sorted(todos.items())))])
        clave = f"{_enc(self.cred['api_secret'])}&{_enc(self.cred['access_secret'])}"
        o["oauth_signature"] = base64.b64encode(hmac.new(clave.encode(), base.encode(), hashlib.sha1).digest()).decode()
        return "OAuth " + ", ".join(f'{_enc(k)}="{_enc(v)}"' for k, v in sorted(o.items()))

    def _post(self, texto):
        cuerpo = json.dumps({"text": texto}).encode()
        req = urllib.request.Request(URL_POST, cuerpo, method="POST", headers={
            "Authorization": self._cabecera("POST", URL_POST), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    def yo(self):
        """Comprueba las credenciales (lectura de la cuenta, no publica)."""
        req = urllib.request.Request(URL_YO, headers={"Authorization": self._cabecera("GET", URL_YO)})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["data"]

    # ---------- cola
    def publicar(self, texto):
        if not self.activo:
            return
        dia = time.strftime("%Y-%m-%d", time.gmtime())
        d, n = self._hoy
        n = n if d == dia else 0
        if n >= self.max_dia:
            log.warning("X: tope diario de %s posts alcanzado, no publico: %s", self.max_dia, texto[:80])
            return
        self._hoy = (dia, n + 1)
        self._cola.put(texto[:280])

    def _enviador(self):
        while True:
            texto = self._cola.get()
            for intento in range(4):
                try:
                    r = self._post(texto)
                    log.info("X publicado %s", r.get("data", {}).get("id"))
                    break
                except urllib.error.HTTPError as e:
                    det = e.read().decode(errors="ignore")[:300]
                    if e.code == 429:
                        reset = int(e.headers.get("x-rate-limit-reset", time.time() + 900))
                        time.sleep(min(max(reset - time.time(), 60), 3600))
                        continue
                    log.error("X %s: %s", e.code, det)
                    if e.code in (401, 402, 403):
                        self.avisar("alta", f"X no aceptó el post ({e.code}): {det[:200]}. Revisá credenciales o créditos.")
                    break
                except Exception as e:
                    log.warning("X: %s", e)
                    time.sleep(30 * (intento + 1))
            time.sleep(2)


# ------------------------------------------------------------ textos
def _encabezado(cfg, t):
    return f"Cascada{' · SIMULACIÓN' if cfg.modo == 'papel' else ''} · {pd.Timestamp(t):%d/%m %H:%M} UTC"


def _resumen_corto(k):
    if "rend_total" not in k:
        return ""
    return (f"📈 Desde el inicio {_pct(k['rend_total'])} · mes {_pct(k.get('rend_mes'))} · "
            f"caída máx {_pct(k.get('caida_max'))}")


def texto_operaciones(db, cfg, desde_ms, hasta_ms, kpis):
    """Post con las aperturas y cierres entre desde_ms (excl.) y hasta_ms (incl.). None si no hubo nada."""
    ab = db.filas("SELECT * FROM lotes WHERE abierto_ts>? AND abierto_ts<=? ORDER BY estrategia", (desde_ms, hasta_ms))
    ce = db.filas("SELECT * FROM lotes WHERE cerrado_ts>? AND cerrado_ts<=? ORDER BY estrategia", (desde_ms, hasta_ms))
    bal = db.filas("SELECT motivo, precio FROM operaciones WHERE cuenta='balas' AND ts>? AND ts<=? ORDER BY id",
                   (desde_ms, hasta_ms))
    if not ab and not ce and not bal:
        return None
    lado = lambda L: "largo" if L["lado"] > 0 else "corto"
    lin = []
    if ab:
        grupos = {}
        for L in ab:
            grupos.setdefault(L["estrategia"], []).append(f"{lado(L)} {L['simbolo']}")
        lin.append("🟢 Abre: " + "; ".join(f"{', '.join(v)} ({NOMBRES.get(e, e)})" for e, v in grupos.items()))
    if ce:
        items = []
        for L in ce:
            n = abs(L["contratos"]) * L["tam_contrato"] * L["precio_entrada"]
            r = (L["pnl"] or 0) / n if n else None
            mot = {"stop": " stop", "corte": " corte", "tope": " recorte", "sin_stop": " sin stop", "asignacion": " sin capital"}.get(L["motivo_salida"], "")
            items.append(f"{lado(L)} {L['simbolo']} {_pct(r)}{mot}")
        lin.append("🔴 Cierra: " + " · ".join(items))
    for b in bal:
        m = b["motivo"]
        if m == "entrada":
            lin.append(f"🎯 30 balas: nueva campaña en BTC a {b['precio']:,.0f}".replace(",", "."))
        elif m in ("salida", "corte", "manual"):
            lin.append("🎯 30 balas: cierre de campaña")
        elif m == "liquidacion":
            lin.append("🎯 30 balas: liquidación")
        elif m.startswith("recarga"):
            lin.append(f"🎯 30 balas: {m}")
    cab = "🔁 " + _encabezado(cfg, pd.to_datetime(hasta_ms, unit="ms"))
    pie = _resumen_corto(kpis)
    texto = "\n".join([cab, *lin, pie]).strip()
    while len(texto) > 270 and len(lin) > 0:      # recorta el detalle si no entra
        lin[-1] = lin[-1][: max(20, len(lin[-1]) - (len(texto) - 267))] + "…"
        texto = "\n".join([cab, *lin, pie]).strip()
        if len(texto) > 270:
            lin.pop()
            texto = "\n".join([cab, *lin, pie]).strip()
    return texto[:270]


def texto_resumen_diario(db, cfg, kpis, t=None):
    if "rend_total" not in kpis:
        return None
    t = pd.Timestamp(t) if t is not None else pd.Timestamp.now("UTC").tz_localize(None)
    r = db.filas("SELECT COUNT(*) n, SUM(pnl>0) g FROM lotes WHERE cerrado_ts IS NOT NULL")[0]
    abiertos = db.filas("SELECT COUNT(*) n FROM lotes WHERE cerrado_ts IS NULL")[0]["n"]
    b = db.get("balas") or {}
    abiertos += int(bool(b.get("activo")))
    from .estadisticas import valor_cuota
    q = valor_cuota(db, "total")
    hoy0 = t.normalize()
    a, b0 = q[q.index <= hoy0], q[q.index <= hoy0 - pd.Timedelta(days=1)]
    dia = float(a.iloc[-1] / (b0.iloc[-1] if len(b0) else q.iloc[0]) - 1) if len(a) else None
    gan = f" · ganadoras {(r['g'] or 0) / r['n']:.0%}" if r["n"] else ""
    lin = [f"📊 Cascada{' · SIMULACIÓN' if cfg.modo == 'papel' else ''} · cierre del {(t - pd.Timedelta(hours=1)):%d/%m}",
           f"Día {_pct(dia, 2)} · semana {_pct(kpis.get('rend_semana'))} · mes {_pct(kpis.get('rend_mes'))}",
           f"Desde el inicio {_pct(kpis['rend_total'])} ({int(kpis.get('dias', 0))} días)",
           f"Caída actual {_pct(kpis.get('caida_actual'))} · máxima {_pct(kpis.get('caida_max'))}",
           f"Operaciones cerradas {r['n']}{gan} · abiertas {abiertos}"]
    return "\n".join(lin)[:280]
