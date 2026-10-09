"""Telegram: avisos (salida) y comandos (entrada) con la API HTTP de bots, sin librerías externas.

Los comandos no se ejecutan en este hilo: se encolan y el bucle principal los atiende entre ciclos,
así nunca se pisan con una decisión del motor. Sólo se aceptan mensajes del chat configurado.
"""
import json
import logging
import queue
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("sistema3.telegram")

ICONO = {"critica": "🚨", "alta": "⚠️", "media": "🔸", "op": "🔁", "info": "ℹ️", "resumen": "📊", "pregunta": "❓"}

AYUDA = """Comandos:
/estado – capital, modo del día y posiciones
/pendiente – la decisión que espera confirmación
/si ID – ejecutar la decisión ID
/no ID – descartar la decisión ID
/pausar – no ejecuta nada nuevo (las decisiones se calculan y se registran)
/reanudar – vuelve a ejecutar
/resolver – marca las incidencias como resueltas (después de revisarlas)
/ayuda – esta lista"""


class Telegram:
    def __init__(self, token, chat_id, niveles=("critica", "alta", "media", "op", "info", "resumen", "pregunta")):
        self.token = token; self.chat = str(chat_id or ""); self.niveles = set(niveles)
        self.activo = bool(token and chat_id)
        self.comandos = queue.Queue()
        self._offset = None
        self._cola = queue.Queue()
        if self.activo:
            threading.Thread(target=self._enviador, daemon=True).start()

    def _api(self, metodo, datos=None, timeout=40):
        url = f"https://api.telegram.org/bot{self.token}/{metodo}"
        cuerpo = urllib.parse.urlencode(datos or {}).encode()
        with urllib.request.urlopen(urllib.request.Request(url, cuerpo), timeout=timeout) as r:
            return json.load(r)

    # ---------- salida
    def avisar(self, nivel, texto):
        log.info("[%s] %s", nivel, texto)
        if self.activo and nivel in self.niveles:
            self._cola.put(f"{ICONO.get(nivel, '')} {texto}".strip())

    def _enviador(self):
        """Envía en orden y con pausa mínima entre mensajes (límite de Telegram ~1 msg/s por chat)."""
        while True:
            texto = self._cola.get()
            for intento in range(5):
                try:
                    self._api("sendMessage", dict(chat_id=self.chat, text=texto[:4000], disable_web_page_preview="true"), 20)
                    break
                except Exception as e:
                    log.warning("telegram envío: %s", e)
                    time.sleep(3 * (intento + 1))
            time.sleep(1.1)

    # ---------- entrada
    def escuchar(self):
        if self.activo:
            threading.Thread(target=self._sondeo, daemon=True).start()

    def _sondeo(self):
        while True:
            try:
                d = dict(timeout=30, allowed_updates=json.dumps(["message"]))
                if self._offset:
                    d["offset"] = self._offset
                r = self._api("getUpdates", d, timeout=40)
                for u in r.get("result", []):
                    self._offset = u["update_id"] + 1
                    m = u.get("message") or {}
                    if str(m.get("chat", {}).get("id")) != self.chat:
                        continue            # ignora a cualquier otro chat
                    texto = (m.get("text") or "").strip()
                    if texto.startswith("/"):
                        partes = texto[1:].split()
                        self.comandos.put((partes[0].split("@")[0].lower(), partes[1:]))
            except Exception as e:
                log.warning("telegram sondeo: %s", e)
                time.sleep(10)
