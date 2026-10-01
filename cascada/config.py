"""Configuración: config/nivel.yaml + variables de entorno (.env)."""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parent.parent
DATOS = Path(os.environ.get("CASCADA_DATOS", RAIZ / "datos"))

# Pesos del doc «Cartera combinada con capital compartido», optimizados para que el peor punto
# intrabarra (velas 4h) no pase el techo. Fracción del patrimonio total que pide cada estrategia al operar.
NIVELES = {
    "techo_intrabarra_10": dict(ab_cortos=0.208, mom_alts=0.125, balas5=0.078, rsi2_btc=0.043, wr2=0.099, sold_btc=0.038, rsi2_eth=0.078),
    "techo_intrabarra_20": dict(ab_cortos=0.426, mom_alts=0.319, balas5=0.191, rsi2_btc=0.084, wr2=0.180, sold_btc=0.107, rsi2_eth=0.096),
    "techo_intrabarra_25": dict(ab_cortos=0.564, mom_alts=0.423, balas5=0.129, rsi2_btc=0.086, wr2=0.467, sold_btc=0.101, rsi2_eth=0.127),
    "techo_intrabarra_30": dict(ab_cortos=0.650, mom_alts=0.468, balas5=0.340, rsi2_btc=0.053, wr2=0.934, sold_btc=0.066, rsi2_eth=0.960),
}
TECHO = {"techo_intrabarra_10": 0.10, "techo_intrabarra_20": 0.20, "techo_intrabarra_25": 0.25, "techo_intrabarra_30": 0.30}
PRIORIDAD = ["ab_cortos", "mom_alts", "balas5", "rsi2_btc", "wr2", "sold_btc", "rsi2_eth"]


@dataclass
class Config:
    modo: str = "papel"                    # papel | real
    nivel: str = "techo_intrabarra_20"
    pesos: dict = field(default_factory=dict)
    prioridad: list = field(default_factory=lambda: list(PRIORIDAD))
    desactivadas: list = field(default_factory=list)   # estrategias apagadas (su peso no se reasigna)
    tope_nocional: float = 1.0
    alerta_caida: float = 0.20
    corte_caida: float = 0.30
    reajuste_fraccion: float = 0.10
    reajuste_usdt: float = 25.0
    apalancamiento_exchange: int = 3
    capital_papel: float = 3000.0
    balas_real: bool = False               # en modo real, 30 balas opera sólo si esto es true y hay claves de la subcuenta
    capital_balas_real: float = 0.0        # USD depositados en la subcuenta de balas (contabilidad inicial)
    comision: float = 0.0006               # taker KuCoin futuros por lado (estimada)
    deslizamiento_max: float = 0.005
    cmc_api_key: str = ""
    telegram_token: str = ""
    telegram_chat: str = ""
    web_usuario: str = "admin"
    web_clave: str = ""
    x_publicar: bool = False               # publicar operaciones y resumen diario en X
    x_operaciones: bool = True
    x_resumen_diario: bool = True
    x_max_dia: int = 12                     # tope de posts por día (X cobra por post)
    x: dict = field(default_factory=dict)
    kucoin: dict = field(default_factory=dict)
    kucoin_balas: dict = field(default_factory=dict)

    @property
    def techo(self):
        return TECHO.get(self.nivel, 0.20)


def ruta_base(cfg):
    """Una base por modo: la historia de papel queda guardada al pasar a real."""
    return DATOS / f"cascada_{cfg.modo}.db"


def cargar(ruta: str | Path | None = None) -> Config:
    ruta = Path(ruta or RAIZ / "config" / "nivel.yaml")
    datos = yaml.safe_load(ruta.read_text()) if ruta.exists() else {}
    c = Config(**{k: v for k, v in (datos or {}).items() if k in Config.__dataclass_fields__})
    if not c.pesos:
        c.pesos = dict(NIVELES[c.nivel])
    for k in c.desactivadas:
        c.pesos[k] = 0.0
    if "corte_caida" not in (datos or {}):
        c.corte_caida = round(1.5 * c.techo, 3)
    if "alerta_caida" not in (datos or {}):
        c.alerta_caida = c.techo
    e = os.environ
    c.cmc_api_key = e.get("CMC_API_KEY", c.cmc_api_key)
    c.telegram_token = e.get("TELEGRAM_TOKEN", c.telegram_token)
    c.telegram_chat = e.get("TELEGRAM_CHAT_ID", c.telegram_chat)
    c.web_usuario = e.get("WEB_USUARIO", c.web_usuario)
    c.web_clave = e.get("WEB_CLAVE", c.web_clave)
    c.x = dict(api_key=e.get("X_API_KEY", ""), api_secret=e.get("X_API_SECRET", ""),
               access_token=e.get("X_ACCESS_TOKEN", ""), access_secret=e.get("X_ACCESS_SECRET", ""))
    c.kucoin = dict(apiKey=e.get("KUCOIN_KEY", ""), secret=e.get("KUCOIN_SECRET", ""), password=e.get("KUCOIN_PASSPHRASE", ""))
    c.kucoin_balas = dict(apiKey=e.get("KUCOIN_BALAS_KEY", ""), secret=e.get("KUCOIN_BALAS_SECRET", ""),
                          password=e.get("KUCOIN_BALAS_PASSPHRASE", ""))
    return c
