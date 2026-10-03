"""Configuración: config/nivel.yaml + variables de entorno (.env)."""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parent.parent
DATOS = Path(os.environ.get("CASCADA_DATOS", RAIZ / "datos"))

# Pesos validados y congelados (validacion/pesos_congelados.json, commit 54ddf76; protocolo validacion/PROTOCOLO.md).
# Fracción del patrimonio total que pide cada estrategia al operar. Elegidos sólo con 2020–2023 y probados una vez en
# 2024-01 → 2026-09. tests/test_config.py verifica que coincidan con el archivo congelado.
NIVELES = {
    "nivel_10": dict(ab_cortos=0.07275, mom_alts=0.0, balas5=0.0485, rsi2_btc=0.07275, wr2=0.0, sold_btc=0.0, rsi2_eth=0.07275),
    "nivel_20": dict(ab_cortos=0.115, mom_alts=0.023, balas5=0.207, rsi2_btc=0.023, wr2=0.0, sold_btc=0.069, rsi2_eth=0.115),
    "nivel_25": dict(ab_cortos=0.141, mom_alts=0.0235, balas5=0.235, rsi2_btc=0.141, wr2=0.047, sold_btc=0.047, rsi2_eth=0.1175),
    "nivel_30": dict(ab_cortos=0.20925, mom_alts=0.0465, balas5=0.34875, rsi2_btc=0.06975, wr2=0.0, sold_btc=0.20925, rsi2_eth=0.11625),
}
# Percentil 95 de la caída máxima en 2000 remuestreos de 2020–2023 (motor completo, 3000 USDT con contratos reales).
P95_IS = {"nivel_10": 0.0936, "nivel_20": 0.192, "nivel_25": 0.2423, "nivel_30": 0.2962}
# Alerta y corte por defecto si nivel.yaml no los fija: corte ≈ p95 de IS (pasarlo indica que algo dejó de comportarse
# como en el backtest), alerta = 2/3 del corte.
CORTE = {"nivel_10": 0.10, "nivel_20": 0.20, "nivel_25": 0.25, "nivel_30": 0.30}
VIEJOS = {f"techo_intrabarra_{n}": f"nivel_{n}" for n in (10, 20, 25, 30)}   # nombres anteriores a la validación
PRIORIDAD = ["ab_cortos", "mom_alts", "balas5", "rsi2_btc", "wr2", "sold_btc", "rsi2_eth"]


@dataclass
class Config:
    modo: str = "papel"                    # papel | real
    nivel: str = "nivel_30"
    pesos: dict = field(default_factory=dict)
    prioridad: list = field(default_factory=lambda: list(PRIORIDAD))
    desactivadas: list = field(default_factory=list)   # estrategias apagadas (su peso no se reasigna)
    tope_nocional: float = 1.0
    tope_con_balas_real: bool = True       # el tope descuenta el nocional real de 30 balas (M7), como en la validación
    alerta_caida: float = 0.20
    corte_caida: float = 0.30
    reajuste_fraccion: float = 0.10
    reajuste_usdt: float = 25.0
    apalancamiento_exchange: int = 3
    capital_papel: float = 3000.0
    balas_real: bool = False               # en modo real, 30 balas opera sólo si esto es true y hay claves de la subcuenta
    capital_balas_real: float = 0.0        # USD depositados en la subcuenta de balas (contabilidad inicial)
    balas_transferir: bool = False         # real: iguala la subcuenta a peso × patrimonio entre campañas (E4)
    kucoin_balas_uid: str = ""             # UID de la subcuenta (KUCOIN_BALAS_UID en .env)
    comision: float = 0.0006               # taker KuCoin futuros por lado (estimada)
    # deslizamiento del modo papel por lado, el mismo de la validación (BTC y ETH 0,05 %, el resto 0,10 %)
    deslizamiento_papel: dict = field(default_factory=lambda: {"BTC": 0.0005, "ETH": 0.0005, "_": 0.0010})
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
    def p95_is(self):
        return P95_IS.get(self.nivel)


def ruta_base(cfg):
    """Una base por modo: la historia de papel queda guardada al pasar a real."""
    return DATOS / f"cascada_{cfg.modo}.db"


def cargar(ruta: str | Path | None = None) -> Config:
    ruta = Path(ruta or RAIZ / "config" / "nivel.yaml")
    datos = yaml.safe_load(ruta.read_text()) if ruta.exists() else {}
    c = Config(**{k: v for k, v in (datos or {}).items() if k in Config.__dataclass_fields__})
    c.nivel = VIEJOS.get(c.nivel, c.nivel)
    if c.nivel not in NIVELES and not c.pesos:
        raise SystemExit(f"Nivel desconocido en config/nivel.yaml: {c.nivel}. Opciones: {', '.join(NIVELES)}")
    if not c.pesos:
        c.pesos = dict(NIVELES[c.nivel])
    for k in c.desactivadas:
        c.pesos[k] = 0.0
    if "corte_caida" not in (datos or {}):
        c.corte_caida = CORTE.get(c.nivel, 0.30)
    if "alerta_caida" not in (datos or {}):
        c.alerta_caida = round(2 / 3 * c.corte_caida, 2)
    e = os.environ
    c.cmc_api_key = e.get("CMC_API_KEY", c.cmc_api_key)
    c.telegram_token = e.get("TELEGRAM_TOKEN", c.telegram_token)
    c.telegram_chat = e.get("TELEGRAM_CHAT_ID", c.telegram_chat)
    c.web_usuario = e.get("WEB_USUARIO", c.web_usuario)
    c.web_clave = e.get("WEB_CLAVE", c.web_clave)
    c.x = dict(api_key=e.get("X_API_KEY", ""), api_secret=e.get("X_API_SECRET", ""),
               access_token=e.get("X_ACCESS_TOKEN", ""), access_secret=e.get("X_ACCESS_SECRET", ""))
    c.kucoin = dict(apiKey=e.get("KUCOIN_KEY", ""), secret=e.get("KUCOIN_SECRET", ""), password=e.get("KUCOIN_PASSPHRASE", ""))
    c.kucoin_balas_uid = e.get("KUCOIN_BALAS_UID", c.kucoin_balas_uid)
    c.kucoin_balas = dict(apiKey=e.get("KUCOIN_BALAS_KEY", ""), secret=e.get("KUCOIN_BALAS_SECRET", ""),
                          password=e.get("KUCOIN_BALAS_PASSPHRASE", ""))
    return c
