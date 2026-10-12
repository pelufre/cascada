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

---

# 30 balas 10x (servicio `balas10`)

Corre al lado del Sistema 3, en la **misma cuenta de KuCoin y con las mismas claves**, y no se mezclan:
- `balas10` sólo usa la posición **XBTUSDM** (futuro inverso, margen **aislado**) y el **BTC** de la cuenta de futuros.
  Nunca compra ni vende en spot y nunca mueve USDT.
- El Sistema 3 sólo cuenta USDT (spot y futuros) y posiciones USDT-M; no ve la posición inversa ni el BTC de futuros.
- Por eso todo el capital de 30 balas que esté en el exchange tiene que estar **en BTC dentro de FUTUROS**.

Reglas (fijas, `balas10/senales.py`; estudio `Balas10x_Segundo_Cargador_2026-10-09.md`, configuración D):
capital de la campaña = BTC en futuros + BTC declarado afuera; cargador 1 = 40 % (30 balas a 10x), cargador 2 = 40 %
(reserva de margen: mitad al abrir, resto si el precio queda a ≤ 12 % de la liquidación), cargador 3 = 20 % (30 balas
que entran cuando el 1 está agotado y el ROE de la campaña es < −10 %, y van **enteras a margen**). Entrada: régimen semanal + RSI(2) 4h ≤ 15 + momento 90 d. Salida: cruce bajo la
SMA80 4h (después de haber estado arriba) o régimen apagado al cierre diario. Señales con velas de Binance BTCUSDT.

**Modo por defecto: todo automático** (`confirmar: false` en `config/balas10.yaml`): abre, recarga y cierra sin preguntar
y avisa por Telegram después de cada operación. Con `confirmar: true`:
**qué hace solo:** todos los aportes de margen (cargador 2, balas sólo-margen, cargador 3, emergencia a ≤ 5 % de la
liquidación con todo el BTC libre). **Qué pregunta (`/si ID`):** abrir, comprar contratos de una recarga y cerrar.
Si el cargador 3 está afuera, avisa cuánto depositar en FUTUROS; al llegar el depósito lo agrega solo (control cada 15 min).

## Instalar (con el Sistema 3 ya funcionando)

1. En KuCoin: la posición XBTUSDM en margen **aislado**; el cargador 2 en **BTC en la cuenta de futuros** (libre).
2. Bot de Telegram propio (para poder responder `/si`): @BotFather → `/newbot`. En `servidor/.env`:
   `TELEGRAM_TOKEN_BALAS=…` (el chat sigue siendo `TELEGRAM_CHAT_ID`). Sin bot propio avisa por el del Sistema 3 y
   se confirma con `python -m balas10.cli aprobar ID`.
3. Bajar el código y construir **sólo** `balas10` (el Sistema 3 sigue corriendo, no se reinicia):
   ```sh
   cd /root/sistema3 && git fetch && git checkout balas10 && git pull
   cd servidor && docker compose build balas10
   docker compose run --rm balas10 python -m balas10.pruebas      # 36 comprobaciones sin red
   docker compose run --rm balas10 python -m balas10.cli verificar
   docker compose run --rm balas10 python -m balas10.cli plan
   ```
4. Adoptar la campaña abierta (primero sin `--ejecutar` para ver el plan):
   ```sh
   docker compose run --rm balas10 python -m balas10.cli adoptar --c1 0.25 --c2 0.25 --c3-afuera 0.125 \
       --balas 30 --desde AAAA-MM-DD
   docker compose run --rm balas10 python -m balas10.cli adoptar --c1 0.25 --c2 0.25 --c3-afuera 0.125 \
       --balas 30 --desde AAAA-MM-DD --ejecutar
   ```
   `--desde` = día (UTC) en que empezó la campaña: sirve para saber si el precio ya cerró sobre la SMA80 4h (sin eso no
   se activa la salida por SMA). Al ejecutar entra la mitad del cargador 2 como margen (regla de la configuración).
5. Arrancar: `docker compose up -d balas10` y en Telegram `/estado`.

Comandos de Telegram: `/estado`, `/pendiente`, `/si ID`, `/no ID`, `/afuera X`, `/pausar`, `/reanudar`, `/ayuda`.
Al depositar el cargador 3 (o parte) en FUTUROS, actualizar `/afuera` con lo que sigue afuera.
Logs: `docker compose logs -f balas10`. Configuración: `config/balas10.yaml`.

**Web:** la pestaña «30 balas» de la web del Sistema 3 muestra posición, liquidación, cargadores, señal, decisiones y
órdenes. El bot guarda una foto de la cuenta cada 2 minutos en su base y la web la lee en sólo lectura (comparten
`servidor/datos`). La pestaña es parte del servicio `sistema3`: para verla después de actualizar el código hay que
reconstruir y reiniciar los dos (`docker compose up -d --build balas10 sistema3`).

**En KuCoin no activar el "depósito automático de margen"** de la posición: el bot lo apaga si lo encuentra activo.

Diferencias con el backtest: el capital entre campañas queda en BTC (el backtest lo tenía en USDT): suma exposición a
BTC cuando no hay posición. El cargador 2 se controla cada 15 min con el precio de marca (el backtest, cada 4 h). Las
decisiones con confirmación se ejecutan al precio del momento en que se aprueban.
