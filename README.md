# Cascada

Sistema que opera en KuCoin la cartera combinada del proyecto «Cortos y Largos Cripto»: siete estrategias que comparten
el capital con prioridades (cortos Aberration, momentum alts c40, 30 balas 5x, RSI(2) BTC, WR2 BTC, Soldados BTC,
RSI(2) ETH). Decide cada 4 horas al cierre de vela, controla el riesgo cada 15 minutos, muestra todo en una web y
avisa por Telegram.

| Nivel | CAGR backtest 2019→sep 2026 | Peor caída intrabarra |
|---|---|---|
| `techo_intrabarra_10` | 36 % | −10 % |
| `techo_intrabarra_20` (por defecto) | 83 % | −20 % |
| `techo_intrabarra_25` | 115 % | −25 % |
| `techo_intrabarra_30` | 184 % | −30 % |

> El backtest no es una promesa. Empezar siempre en **modo papel** unas 4 semanas y comparar en la pestaña Backtest
> que la curva en vivo quede dentro de la banda esperada.

## Capital

- **Mínimo: 3000 USD**, con Soldados apagado (viene así en `config/nivel.yaml`): ≈ 2430 en la cuenta principal de futuros
  USDT-M y ≈ 570 en la subcuenta de 30 balas (nivel 20).
- **Recomendado: 5000 USD o más**, para que todos los lotes superen el contrato mínimo de KuCoin (sobre todo BTC) y se
  pueda encender Soldados.
- La web avisa cuando un lote quedó bajo el mínimo y no se abrió.

## Instalación paso a paso (desde el celular con Termux)

### 0. Antes de empezar
1. Si había otro bot en el servidor: **cerrar sus posiciones en KuCoin**, borrar sus claves de API en KuCoin y apagarlo
   (`docker ps` y `docker stop …`, o `systemctl stop …`, según cómo estaba instalado).
2. Tener a mano:
   - Una clave de API de **CoinMarketCap** (plan gratuito): <https://pro.coinmarketcap.com/signup>.
   - Un bot de **Telegram**: hablarle a `@BotFather`, `/newbot`, y guardar el token.
   - Un token de GitHub para bajar este repositorio privado: GitHub → Settings → Developer settings →
     Fine-grained tokens → acceso sólo a `pelufre/cascada`, permiso *Contents: Read-only*.

### 1. Entrar al servidor
```sh
pkg install openssh        # sólo la primera vez en Termux
ssh root@IP_DEL_SERVIDOR
```

### 2. Bajar el código
```sh
apt update && apt install -y git
git clone https://github.com/pelufre/cascada.git /root/cascada
# usuario: pelufre   ·   contraseña: el token de GitHub (no tu clave)
cd /root/cascada
```

### 3. Instalar
```sh
./instalar.sh
```
La primera vez crea `.env` y pide completar `CMC_API_KEY` y `TELEGRAM_TOKEN`:
```sh
nano .env          # completar, guardar con Ctrl+O, Enter, salir con Ctrl+X
./instalar.sh
```
Después, escribirle cualquier cosa al bot de Telegram y obtener el chat:
```sh
docker compose exec cascada python -m cascada.cli telegram-chat
nano .env          # poner el número en TELEGRAM_CHAT_ID
docker compose up -d
```
Al terminar muestra la dirección de la web (`https://<ip>.sslip.io`), el usuario y la clave.

### 4. Comprobar
```sh
docker compose exec cascada python -m cascada.cli verificar
```
Muestra el tamaño real de contrato de BTC y ETH en KuCoin, prueba CoinMarketCap y manda un mensaje de prueba a Telegram.

## La web

- **Resumen**: patrimonio, rendimiento de hoy, semana, mes y total, CAGR, caída actual y máxima, Sharpe, curva de
  patrimonio (total, principal y balas), caída desde el máximo y capital pedido/concedido por estrategia.
- **Posiciones**: cada lote abierto con entrada, precio, stop y resultado; la campaña de 30 balas con balas usadas,
  distancia a la liquidación y reserva.
- **Operaciones**: lotes cerrados y órdenes, con filtro por estrategia.
- **Rendimiento**: mensual, semanal y diario.
- **Estrategias**: peso, capital pedido y concedido, lotes, % ganadoras y resultado.
- **Incidencias**: lo que pasó y **lo que puede pasar** (cerca del corte o de la alerta, stops cerca, funding en contra,
  tope de capital, lotes bajo mínimo, cambio del top 50, liquidación de balas cerca, conciliación).
- **Backtest**: curva en vivo contra la banda 5–95 % del backtest para el mismo plazo; curvas 2019→2026 por nivel y por
  estrategia; tabla con CAGR, caídas y años.
- Botones **Pausar** y **Reanudar**.

## Telegram

Avisos de entradas, salidas, stops, alertas, incidencias previstas graves y un **resumen diario** a las 00:00 UTC.
Comandos: `/estado`, `/previsiones`, `/incidencias`, `/pausar`, `/reanudar`, `/cerrar_todo si`, `/nivel`, `/ayuda`.
Sólo responde al chat configurado.

## X (Twitter)

Con `x_publicar: true` en `config/nivel.yaml`, cada ciclo de 4 h que tenga aperturas o cierres genera **un solo post**
(qué abrió, qué cerró con su resultado y el acumulado desde el inicio), y a las 00:00 UTC un **resumen diario**. En modo
papel cada post dice SIMULACIÓN. Sin enlaces: X cobra bastante más por post con enlace. Tope `x_max_dia` (12).

1. Crear la cuenta en x.com y entrar a <https://developer.x.com> con ella. Crear un *Project* y una *App*.
2. En la App → *User authentication settings*: permisos **Read and write** (tipo *Web App/Automated App*; como
   Callback URL y Website se puede poner `https://<tu DOMINIO>`).
3. En *Keys and tokens*: copiar **API Key** y **API Key Secret**, y generar **Access Token** y **Access Token Secret**
   (generarlos *después* de poner Read and write; si no, quedan de sólo lectura).
4. Cargar créditos en la consola de X (se cobra por post).
5. Completar `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET` en `.env`, poner `x_publicar: true` y:
   `docker compose up -d && docker compose exec cascada python -m cascada.cli x-prueba`

## Riesgo

- Nocional total ≤ 1 × patrimonio (`tope_nocional`); si los precios lo empujan arriba de 1,05 ×, recorta desde la última
  prioridad.
- **Alerta** al llegar a la caída del techo del nivel (20 % en el nivel 20); **corte** a 1,5 × techo (30 %): cierra todo y
  bloquea entradas hasta `/reanudar`.
- Conciliación en cada ciclo: si el libro no coincide con las posiciones de KuCoin, no abre nada nuevo y avisa.
- `/pausar` deja de abrir lotes; los abiertos siguen con sus salidas y stops.

## Pasar a real (después de ~4 semanas en papel)

1. En KuCoin crear la **subcuenta** para 30 balas. Crear claves de API **nuevas** para la cuenta principal y para la
   subcuenta: permiso de **trading de futuros** (y General), **sin retiros**, **restringidas a la IP del servidor**.
2. Transferir el capital: ≈ 81 % a futuros USDT-M de la cuenta principal y ≈ 19 % a la subcuenta (nivel 20).
3. Completar las claves en `.env`. En `config/nivel.yaml`: `modo: real`; para 30 balas `balas_real: true` y
   `capital_balas_real:` con lo depositado. Recomendado: arrancar unos días con `balas_real: false` y montos chicos.
4. La cuenta real usa su propia base (`datos/cascada_real.db`); la historia de papel queda guardada en
   `datos/cascada_papel.db` y vuelve a verse si se pone `modo: papel` otra vez.
5. `docker compose up -d` y `cli verificar`.
6. Aportes o retiros posteriores: `docker compose exec cascada python -m cascada.cli aporte 500` (o `-500`) para que no
   cuenten como ganancia ni como caída.

## Mantenimiento

```sh
cd /root/cascada
docker compose logs -f cascada                 # registros en vivo
git pull && docker compose up -d --build       # actualizar
docker compose restart cascada                 # después de cambiar config/nivel.yaml o .env
crontab -e   # agregar:  15 3 * * * /root/cascada/scripts/respaldo.sh   (copia diaria de la base, 30 días)
```

## Estructura

```
cascada/
  principal.py     servicio: ciclo de 4 h, controles de 15 min, resumen diario, comandos
  motor.py         cuenta principal: estrategias → asignador → órdenes → stops → conciliación
  estrategias/     RSI(2) BTC/ETH, WR2, Soldados, cortos Aberration, momentum c40
  asignador.py     reparto del capital por prioridad (cascada)
  balas.py         30 balas 5x en la subcuenta (futuro inverso XBTUSDM)
  riesgo.py        caída, corte, tope, conciliación y previsión de incidencias
  bolsa.py         KuCoin real (ccxt) y exchange simulado (papel)
  datos.py         velas 4h incrementales y top 50 semanal de CoinMarketCap
  web.py, estatico/ web
  telegram.py      avisos y comandos
  cli.py           verificar, estado, aporte, telegram-chat, reiniciar-papel
datos_backtest/    curvas del backtest para la web
tests/             paridad con el backtest y simulación de punta a punta
```

## Pruebas hechas

- **Paridad de la cuenta principal**: el motor en papel, vela por vela de 2025-01 a 2026-09, coincide con el backtest en
  los días en mercado (cortos 100 %, momentum 98,9 %, RSI(2) ETH 99,2 %, WR2 97,8 %, RSI(2) BTC 95,9 %, Soldados 94,2 %).
- **Paridad de 30 balas**: 2019→2026, mismas 279 campañas y 0 liquidaciones que el backtest; patrimonio final 22,12 vs
  22,08 (correlación diaria 0,9999).
- **Simulación de punta a punta** sin internet: ciclos, balas, controles, comandos y todas las rutas de la web.
- Lo que **no** se pudo probar desde acá: las llamadas reales a KuCoin (órdenes, stops, tamaños de contrato). Por eso el
  modo papel primero y `cli verificar` en el servidor.
