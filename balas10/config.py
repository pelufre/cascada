"""Configuración: config/balas10.yaml + variables de entorno (servidor/.env, la misma del Sistema 3)."""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parent.parent
DATOS = Path(os.environ.get("SISTEMA3_DATOS", RAIZ / "datos"))


@dataclass
class Config:
    modo: str = "real"                  # real | papel
    confirmar: bool = True              # abrir, comprar contratos y cerrar esperan /si ID (los aportes de margen no)
    vence_hora_local: str = "11:00"     # una decisión sin respuesta vence la próxima vez que sean estas horas (local)
    vence_h: float = 12.0               # si vence_hora_local está vacío
    zona_horaria: str = "America/Argentina/Buenos_Aires"
    emergencia_dist: float = 0.05       # a esta distancia de la liquidación: aviso crítico cada hora
    emergencia_usar_libre: bool = True  # y todo el BTC libre de futuros entra como margen
    espera_cierre_s: int = 120          # segundos después del cierre de vela antes de decidir
    capital_papel_btc: float = 0.5
    puerto_salud: int = 8081
    telegram_token: str = ""
    telegram_chat: str = ""
    telegram_escuchar: bool = False     # sólo con un bot propio (TELEGRAM_TOKEN_BALAS); el del Sistema 3 ya escucha
    kucoin: dict = field(default_factory=dict)
    parametros: dict = field(default_factory=dict)   # cambios a senales.P (no recomendado)


def ruta_base(cfg):
    return DATOS / f"balas10_{cfg.modo}.db"


def cargar(ruta=None):
    ruta = Path(ruta or RAIZ / "config" / "balas10.yaml")
    datos = yaml.safe_load(ruta.read_text()) if ruta.exists() else {}
    c = Config(**{k: v for k, v in (datos or {}).items() if k in Config.__dataclass_fields__})
    e = os.environ
    propio = e.get("TELEGRAM_TOKEN_BALAS", "")
    c.telegram_token = propio or e.get("TELEGRAM_TOKEN", "")
    c.telegram_escuchar = bool(propio)
    c.telegram_chat = e.get("TELEGRAM_CHAT_ID_BALAS", "") or e.get("TELEGRAM_CHAT_ID", "")
    c.kucoin = dict(apiKey=e.get("KUCOIN_KEY", ""), secret=e.get("KUCOIN_SECRET", ""), password=e.get("KUCOIN_PASSPHRASE", ""))
    return c
