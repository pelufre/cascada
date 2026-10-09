"""Configuración: config/sistema3.yaml + variables de entorno (.env)."""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .senales import NIVELES

RAIZ = Path(__file__).resolve().parent.parent
DATOS = Path(os.environ.get("SISTEMA3_DATOS", RAIZ / "datos"))


@dataclass
class Config:
    modo: str = "real"                  # real | papel
    nivel: str = "moderado"             # conservador | moderado | agresivo (riesgo por alt y % del modo BTC)
    confirmar: bool = True              # real: cada decisión espera el OK por Telegram o la web antes de enviar órdenes
    vence_diaria_h: float = 12.0        # una decisión diaria sin respuesta se descarta pasadas estas horas
    vence_4h_h: float = 3.5             # una decisión de cortos sin respuesta se descarta pasadas estas horas
    capital_papel: float = 3000.0
    apalancamiento_exchange: int = 3    # sólo para la reserva de margen de KuCoin; el riesgo lo fija el nocional
    margen_objetivo: float = 0.60       # saldo de FUTUROS ≥ 60 % del nocional de cortos antes de abrir
    margen_alerta: float = 0.40         # aviso y recarga si cae debajo del 40 %
    espera_cierre_s: int = 90           # segundos después del cierre de vela antes de decidir
    cmc_api_key: str = ""
    telegram_token: str = ""
    telegram_chat: str = ""
    web_usuario: str = "admin"
    web_clave: str = ""
    kucoin: dict = field(default_factory=dict)

    @property
    def riesgo_alt(self):
        return NIVELES[self.nivel]["riesgo_alt"]

    @property
    def pct_btc(self):
        return NIVELES[self.nivel]["btc"]


def ruta_base(cfg):
    return DATOS / f"sistema3_{cfg.modo}.db"


def cargar(ruta=None):
    ruta = Path(ruta or RAIZ / "config" / "sistema3.yaml")
    datos = yaml.safe_load(ruta.read_text()) if ruta.exists() else {}
    c = Config(**{k: v for k, v in (datos or {}).items() if k in Config.__dataclass_fields__})
    if c.nivel not in NIVELES:
        raise SystemExit(f"Nivel desconocido en config/sistema3.yaml: {c.nivel}. Opciones: {', '.join(NIVELES)}")
    e = os.environ
    c.cmc_api_key = e.get("CMC_API_KEY", c.cmc_api_key)
    c.telegram_token = e.get("TELEGRAM_TOKEN", c.telegram_token)
    c.telegram_chat = e.get("TELEGRAM_CHAT_ID", c.telegram_chat)
    c.web_usuario = e.get("WEB_USUARIO", c.web_usuario)
    c.web_clave = e.get("WEB_CLAVE", c.web_clave)
    c.kucoin = dict(apiKey=e.get("KUCOIN_KEY", ""), secret=e.get("KUCOIN_SECRET", ""), password=e.get("KUCOIN_PASSPHRASE", ""))
    return c
