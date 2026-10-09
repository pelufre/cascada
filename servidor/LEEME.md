# Sistema 3 en el servidor

FR20 (largos en alts) + modo BTC + cortos Aberration 0,5×, con capital compartido y regla de conflicto, tal como la
especificación congelada (`Estrategia_FR20_BTC_cortos_especificacion.md`). Las señales se calculan con velas de Binance
(los mismos datos del backtest) y las órdenes van a **KuCoin**: spot para los largos y futuros USDT-M para los cortos.

- Código: `sistema3/` (reglas en `senales.py`, ejecución en `motor.py`, KuCoin en `bolsa.py`).
- Configuración: `config/sistema3.yaml` (modo, nivel de riesgo, confirmación). Claves: `servidor/.env`.
- Pruebas sin red: `python -m sistema3.pruebas`.

## Instalar

```sh
git clone -b sistema3 https://github.com/pelufre/cascada.git /root/sistema3
cd /root/sistema3/servidor
./instalar.sh            # la primera vez crea .env y dice qué falta
nano .env                # CMC_API_KEY, TELEGRAM_TOKEN, KUCOIN_KEY/SECRET/PASSPHRASE
./instalar.sh
docker compose exec sistema3 python -m sistema3.cli telegram-chat   # → TELEGRAM_CHAT_ID en .env
docker compose up -d
docker compose exec sistema3 python -m sistema3.cli verificar
docker compose exec sistema3 python -m sistema3.cli plan           # qué haría ahora, sin operar
```

La clave de KuCoin (mejor en una subcuenta sólo para el sistema): permisos General + Spot + Futuros, **sin retiros**,
restringida a la IP del servidor. Desactivar «pagar comisiones con KCS».

## Cómo opera

- **00:00 UTC**: decisión diaria (modo ALTS / BTC / USDT y la rutina FR20) más la de cortos.
- **Cada 4 h** (00, 04, …, 20 UTC): cortos Aberration.
- Con `confirmar: true` cada decisión con órdenes llega por Telegram y espera `/si ID` (o el botón de la web).
  Sin respuesta vence (12 h la diaria, 3,5 h la de cortos) y no se ejecuta nada.
- Cada 15 minutos registra el capital y controla el margen de futuros.

Telegram: `/estado`, `/pendiente`, `/si ID`, `/no ID`, `/pausar`, `/reanudar`, `/resolver`, `/ayuda`.

## Comandos útiles

```sh
docker compose logs -f sistema3
docker compose exec sistema3 python -m sistema3.cli estado
docker compose exec sistema3 python -m sistema3.cli aporte 500 "depósito"   # para que no cuente como ganancia
git pull && docker compose up -d --build                                    # actualizar
```

## Diferencias con el backtest (conocidas)

- Ejecución en KuCoin: precios, comisiones y liquidez algo distintos de Binance. Sólo se compran o venden en corto las
  monedas que existen en KuCoin; la amplitud se mide igual que en el backtest (top 50 con par en Binance).
- Al arrancar, el estado de la amplitud se reconstruye con 200 días de historia usando el top 50 actual (el backtest
  usaba el de cada semana); desde ahí se guarda día a día.
