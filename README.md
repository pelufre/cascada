# Cascada

Sistema que opera en KuCoin la cartera combinada del proyecto «Cortos y Largos Cripto»: siete estrategias que comparten
el capital con prioridades (cortos Aberration, momentum alts c40, 30 balas 5x, RSI(2) BTC, WR2 BTC, Soldados BTC,
RSI(2) ETH). Decide cada 4 horas al cierre de vela, controla el riesgo cada 15 minutos, muestra todo en una web y
avisa por Telegram.

Pesos validados con metodología pre-registrada (`validacion/PROTOCOLO.md`): elegidos sólo con 2020–2023, congelados
(commit 54ddf76) y probados una vez en 2024-01 → 2026-09. 3000 USDT con contratos reales de KuCoin, costos y funding.

| Nivel | Tasa anual 2024–sep 2026 (prueba) | Caída máx. (prueba) | Tasa 2020–23 (ajuste) | p95 de caída 2020–23 |
|---|---|---|---|---|
| `nivel_10` | 8,4 % | −4,4 % | 16,5 % | 9,4 % |
| `nivel_20` | 16,3 % | −9,0 % | 37,4 % | 19,2 % |
| `nivel_25` | 24,5 % | −11,3 % | 50,6 % | 24,2 % |
| `nivel_30` (por defecto) | 28,7 % | −14,0 % | 68,6 % | 29,6 % |

> El backtest no es una promesa: las cifras de referencia son las del período de prueba, no las de ajuste. Empezar
> siempre en **modo papel** (fase 3 del plan: 8–12 semanas) y comparar cada semana el papel contra el motor
> (`validacion/comparar_papel.py`). El nivel operado (30 %) se eligió antes de abrir 2024–2026; los otros quedan como
> perfiles de riesgo y no se eligen mirando la prueba.

## Capital

- **3000 USD**, con las siete estrategias (la validación usó 3000 USDT con los contratos y mínimos reales de KuCoin).
  En el nivel 30, 30 balas recibe ≈ 35 % del patrimonio al empezar cada campaña y la cuenta principal el resto.
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
- **Backtest**: curva en vivo contra la banda 5–95 % del backtest para el mismo plazo; curvas validadas 2020→2026 por
  nivel y por estrategia; tabla con la tasa de ajuste, la de prueba, caídas y años (`validacion/series_web.py`).
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

## Riesgo y ejecución

- Una sola cuenta con **posición neta por símbolo**. En un símbolo todos los lotes abiertos van del mismo lado; si una
  estrategia quiere abrir del lado contrario mientras otra tiene un lote abierto, se omite y queda como incidencia.
- Cada ciclo: órdenes pendientes → stops → **conciliación antes de operar** → riesgo → estrategias → asignación →
  una orden neta por símbolo → stops → conciliación final.
- El libro registra **lo que llenó el exchange** (parciales incluidos); el resto de una orden parcial se cancela.
- Toda reducción o cierre va **reduce-only**. Las aperturas reales van con límite IOC a ±0,5 % (`deslizamiento_max`).
- Cada lote con stop tiene su stop reduce-only en el exchange. Un stop ejecutado cierra el lote al precio real; uno
  cancelado o perdido se repone; si no se puede poner, el lote se cierra y avisa.
- Nocional total ≤ 1 × patrimonio (`tope_nocional`); si los precios lo empujan arriba de 1,05 ×, recorta desde la última
  prioridad (sobre los objetivos, antes de mandar órdenes).
- **Alerta** al 20 % de caída desde el máximo y **corte** al 30 % (≈ p95 de caída de 2020–23 del nivel 30;
  `alerta_caida` y `corte_caida` en `config/nivel.yaml`). El corte pasa a
  «liquidando», cierra todo (principal y 30 balas) y **reintenta cada minuto hasta quedar plano**; las entradas quedan
  bloqueadas hasta `/reanudar`.
- Si libro y exchange no coinciden, ese ciclo **sólo reduce** y avisa. Si el exchange tiene menos que el libro (stop o
  liquidación que no se pudo leer) el libro se ajusta solo y avisa; si tiene más, hay que revisarlo a mano.
- `/pausar` deja de abrir y de agrandar lotes; los abiertos siguen con sus salidas y stops.
- El control de riesgo corre en **su propio hilo** cada 15 minutos. `/salud` responde 503 si el bucle o el control
  dejaron de latir, el último ciclo tiene más de 4 h 20 min, hay incidencias críticas abiertas, el libro no concilia o
  hay un cierre total pendiente.

## Pasar a real (después de ~4 semanas en papel)

1. En KuCoin crear la **subcuenta** para 30 balas. Crear claves de API **nuevas** para la cuenta principal y para la
   subcuenta: permiso de **trading de futuros** (y General), **sin retiros**, **restringidas a la IP del servidor**.
2. Transferir el capital: ≈ 65 % a futuros USDT-M de la cuenta principal y ≈ 35 % a la subcuenta (nivel 30). En papel
   el capital de 30 balas se iguala a peso × patrimonio al empezar cada campaña; en real todavía no hay transferencia
   automática entre cuentas, así que la subcuenta queda con lo depositado hasta reajustarla a mano.
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

Comparación semanal papel ↔ motor (fase 3): corre el motor de backtest sobre las mismas velas, universo y volúmenes que
tuvo el servicio y lista cada diferencia de entradas, salidas, cantidades y precios (sale con código 1 si hay alguna):
```sh
docker compose exec cascada python -m validacion.comparar_papel
```

## Estructura

```
cascada/
  principal.py     servicio: ciclo de 4 h, control de riesgo en su hilo, resumen diario, comandos
  motor.py         cuenta principal: posición neta por símbolo, órdenes con llenado real, stops, conciliación, corte
  estrategias/     RSI(2) BTC/ETH, WR2, Soldados, cortos Aberration, momentum c40
  asignador.py     reparto del capital por prioridad (cascada)
  balas.py         30 balas 5x en la subcuenta (futuro inverso XBTUSDM)
  riesgo.py        caída y previsión de incidencias
  bolsa.py         KuCoin real (ccxt) y exchange simulado (papel)
  datos.py         velas 4h incrementales, top 50 semanal de CoinMarketCap y volumen diario de los perpetuos
  web.py, estatico/ web
  telegram.py      avisos y comandos
  cli.py           verificar, estado, aporte, telegram-chat, reiniciar-papel
datos_backtest/    curvas del backtest validado para la web
validacion/        protocolo, motor de backtest (el mismo código del servicio), selección de pesos, pesos congelados,
                   evaluación de la prueba y comparación papel ↔ motor
tests/             defectos de la auditoría, servicio (red, reinicios, velas faltantes, balas, funding) y configuración
```

## Pruebas hechas

- **Defectos de la auditoría** (`python -m tests.test_auditoria`): una prueba por cada uno de los 15 defectos operativos
  (llenados parciales, stops rechazados o cancelados, reduce-only, pausa, conciliación, tope, resultado acumulado,
  corte que reintenta, RSI(2) que no reentra, 30 balas con historia y recarga de margen…). 0/15 con la versión 1.0,
  16/16 con la 1.1.
- **Paridad de la cuenta principal**: el motor en papel, vela por vela de 2025-01 a 2026-09, coincide con el backtest en
  los días en mercado (cortos 100 %, momentum 99,2 %, RSI(2) ETH 99,1 %, WR2 97,8 %, RSI(2) BTC 95,9 %, Soldados 94,0 %).
  El motor 1.1 da exactamente los mismos días en mercado que el 1.0 y el mismo patrimonio final (±0,01 %).
- **Servicio** (`python -m tests.test_servicio`): red que se cae antes o después de llenar una orden, reinicio a mitad
  de ciclo, velas y precios faltantes, volumen de los perpetuos, capital de 30 balas por campaña y funding del papel.
- **Configuración** (`python -m tests.test_config`): los pesos del servicio son exactamente los congelados.
- **Filtro de liquidez de momentum (c40)**: mediana de 30 días del volumen diario ≥ 2 M USD, con el volumen en USDT del
  perpetuo (Binance; KuCoin si Binance no lo lista), la misma medida de la validación. La primera vez completa 35 días.
- **Paridad de 30 balas**: 2019→2026, mismas 279 campañas y 0 liquidaciones que el backtest; patrimonio final 22,12 vs
  22,08 (correlación diaria 0,9999).
- **Simulación de punta a punta** sin internet: ciclos, balas, controles, comandos y todas las rutas de la web.
- Lo que **no** se pudo probar desde acá: las llamadas reales a KuCoin (órdenes, stops, tamaños de contrato). Por eso el
  modo papel primero, `cli verificar` y `cli balas-prueba` en el servidor.
