# Protocolo de validación de Cascada (pre-registro)

Escrito el 2026-10-02, **antes** de correr ningún backtest nuevo. El commit que agrega este archivo es el sello: su
hash y su fecha prueban qué se decidió antes de ver resultados. Cualquier cambio posterior va en la sección
«Enmiendas», con fecha, motivo y si se hizo antes o después de abrir el período de prueba.

## 1. Qué se valida

La cartera Cascada tal como opera el servicio en vivo (motor 1.1): siete estrategias con capital compartido por
prioridad. **Las reglas y parámetros de cada estrategia quedan fijos** (código en `cascada/estrategias/__init__.py`
y `cascada/balas.py` en este commit). Lo único que se elige con datos son **los pesos de la cartera** y, en una única
decisión binaria definida abajo, si 30 balas se reemplaza por una estrategia de tendencia simple.

| Estrategia | Reglas fijas (resumen) |
|---|---|
| ab_cortos | Cortos Aberration 4h en el top 50: SMA/σ 120 velas, k = 2, ROC 90 días < 0 del activo y de BTC, 3 cupos, filtro de vela −20 % |
| mom_alts (c40) | Momentum diario en el top 50: amplitud > 60 % / < 40 % sobre SMA50, BTC sobre SMA140 y ROC 84 > 0, score 0,5·ROC7 + 0,3·ROC14 + 0,2·ROC28 ajustado por ATR, 5 cupos, riesgo 2 % con stop 3·ATR, historia ≥ 90 días, liquidez ≥ 2 M USD (mediana 30 días) |
| balas5 | 30 balas 5x en futuro inverso XBTUSDM, parámetros `P` de `balas.py` |
| rsi2_btc / rsi2_eth | RSI(2) 4h ≤ 20 sobre EMA200, salida con EMA300 |
| wr2 | Williams %R(2) diario de BTC con SMA200, DMI y volatilidad objetivo 40 % |
| sold_btc | Soldados BTC: Donchian 20/10 diario, stop 1·ATR, breakeven a +1R |

Prioridad del reparto (fija): ab_cortos, mom_alts, balas5, rsi2_btc, wr2, sold_btc, rsi2_eth.

**Límite reconocido:** las reglas se diseñaron en investigaciones que ya habían visto toda la historia hasta 2026. Por
eso este protocolo deja limpios los pesos y la decisión de balas, no las ideas. La única prueba limpia en todo sentido
es el papel desde hoy en adelante (fase 3), que se evalúa con los mismos criterios.

## 2. Períodos

- **Muestra de ajuste (IS):** 2019-01-01 a 2023-12-31. Todo lo que se elige, se elige sólo con este tramo.
- **Muestra de prueba (OOS):** 2024-01-01 a 2026-09-30. Se abre **una sola vez**, con los pesos ya congelados.
- Los indicadores pueden usar velas anteriores a 2019-01-01 como precalentamiento; los resultados se miden desde el
  primer día de cada tramo.

## 3. Datos

- Velas de 4 h y funding histórico de los **perpetuos USDT-M**: Binance como fuente principal (más historia); KuCoin
  donde Binance no tenga el contrato. Ticker con prefijo 1000 (1000SHIB, 1000PEPE…) se reescala al precio unitario.
- Universo: top 50 semanal de CoinMarketCap (archivo del proyecto) **intersectado** con los símbolos que tenían
  perpetuo listado esa semana (fecha de listado = primera vela del perpetuo). Las ventanas de indicadores usan sólo
  velas del perpetuo; un símbolo sin historia suficiente no es elegible, igual que en vivo.
- Liquidez de c40: mediana de 30 días del volumen en USD **del perpetuo** (Binance). El servicio en vivo pasará a usar
  la misma medida (hoy usa el volumen global de CoinMarketCap).
- 30 balas: precio del perpetuo BTCUSDT de Binance (el inverso XBTUSDM de KuCoin sólo tiene velas desde 2024-08) y
  funding real de XBTUSDM desde 2019-08 (antes, el de BTCUSDT de Binance).
- Contratos de KuCoin (tamaño y mínimo) con las especificaciones actuales; las históricas no están disponibles.
- Todo en un paquete de datos con huella SHA-256 por archivo (`validacion/datos/MANIFIESTO.json`). El cargador corta
  en 2023-12-31 23:59 cuando se trabaja en IS y se niega a devolver fechas posteriores.

## 4. Modelo de ejecución (un solo motor)

- El backtest corre **el mismo código del servicio en vivo** (motor, estrategias, asignador, 30 balas) vela por vela
  contra el exchange simulado. No hay un modelo de investigación aparte.
- Decisión al cierre de cada vela de 4 h; ejecución al **precio de apertura de la vela siguiente** más deslizamiento.
- Stops dentro de la vela: al precio del stop, o a la apertura si la vela abre más allá.
- Costos por lado: comisión taker 0,06 % + deslizamiento 0,05 % en BTC y ETH, 0,10 % en las demás.
- Funding cada 8 h sobre el nocional abierto, con el signo real (el largo paga si la tasa es positiva).
- **Tope total de 1× el patrimonio sumando las dos cuentas**, contando el nocional real de 30 balas (corrige M7).
- 30 balas en su propia cuenta con el capital asignado al empezar cada campaña, sin rebalanceo diario, como opera en
  vivo (corrige M6). Liquidación con los tramos de riesgo de KuCoin.
- Patrimonio marcado a mercado cada 4 h. La caída se informa como rango: optimista (sólo cierres) y pesimista
  (peor punto de cada vela contra el máximo anterior).
- Dos corridas por cada configuración: **A** con capital grande y contratos fraccionarios (la estrategia pura) y
  **B** con 3000 USDT y los contratos y mínimos reales de KuCoin (lo que se va a operar).

## 5. Elección de pesos (sólo IS)

1. Se corre cada estrategia sola en el motor (corrida A, peso 1) sobre IS.
2. Búsqueda de pesos en grilla de 0,025 entre 0 y 1 por estrategia, con el reparto en cascada y el tope total.
   Semilla fija 12345 para todo lo aleatorio.
3. Para cada nivel de riesgo L ∈ {10 %, 20 %, 25 %, 30 %}: maximizar la tasa anual compuesta de IS sujeta a que el
   **percentil 95 de la caída máxima** en 2000 remuestreos por bloques estacionarios (bloque medio de 20 días) de los
   retornos diarios de IS sea ≤ L. Si una estrategia queda con peso 0, sale de ese nivel.
4. Los pesos elegidos se verifican con el motor completo sobre IS (corridas A y B). Si el percentil 95 del motor completo
   supera L, todos los pesos se escalan por el mismo factor hasta cumplirlo. Nada más se ajusta.
5. **Decisión de 30 balas** (antes de los pasos 2–4 definitivos): se repiten los pasos 2–3 reemplazando balas5 por
   «BTC tendencia» (largo 1× en el perpetuo BTCUSDT cuando el cierre diario está sobre su SMA de 200 días, misma
   prioridad). Se queda la variante con mayor tasa anual de IS en el nivel 20 %; si la diferencia es menor a 1 punto
   anual, se queda BTC tendencia por ser más simple.
6. Antes de abrir el OOS, el usuario elige el nivel con que operaría (decisión 4 del plan), mirando sólo IS. Se registra
   en «Enmiendas».
7. Se congelan `validacion/pesos_congelados.json` (pesos por nivel, nivel elegido, variante de balas) con su SHA-256 en
   un commit.

## 6. Evaluación del OOS (una sola vez)

`validacion/evaluar_oos.py` sólo corre si el commit de `pesos_congelados.json` existe y su hash coincide. Corre las
corridas A y B sobre 2024-01-01 → 2026-09-30, guarda `resultado_oos.json` con su hash y anota la ejecución en
`validacion/REGISTRO.md`. Volver a correrlo sólo vale para reproducir los mismos números.

Se informa: tasa anual, caída máxima (rango), Sharpe, Calmar, resultado por año y por estrategia, cantidad de
operaciones, factor de beneficio, peor operación, comisiones y funding pagados.

Comparaciones en el mismo período y el mismo motor:
- BTC comprar y mantener (perpetuo, 1×) y ETH comprar y mantener.
- BTC tendencia (SMA 200 diaria, 1×).
- Pesos iguales por riesgo (inversa de la volatilidad de IS) de las mismas estrategias.
- La cartera sin 30 balas y la cartera sin alts (sin ab_cortos ni mom_alts).

## 7. Criterios de aceptación (fijados ahora)

Con el nivel elegido, corrida B:
- **C1:** tasa anual del OOS > 0, neta de costos y funding.
- **C2:** caída máxima pesimista del OOS ≤ percentil 95 de los remuestreos de IS para ese nivel.
- **C3:** Calmar del OOS ≥ Calmar de BTC tendencia en el mismo OOS.
- Informativo (no decide): Sharpe del OOS frente al de IS.

Si falla cualquiera: **esta configuración no pasa a dinero real**. No se reajusta nada mirando 2024–2026. Un rediseño
vuelve a empezar con este protocolo y su único período de prueba válido es el papel desde la fecha del rediseño.

## 8. Reglas de conducta

- Nadie mira series, gráficos ni métricas de 2024 en adelante hasta el paso 6. Los resultados por año que ya se vieron
  antes de este protocolo quedan reconocidos en la sección 1.
- Todo número que se informe dice de qué tramo sale (IS, OOS o papel) y de qué corrida (A o B).
- Errores de código encontrados después de abrir el OOS: se corrigen, se documentan en «Enmiendas» y se vuelve a correr
  IS y OOS con los **mismos pesos congelados**; los pesos no se tocan.

## Enmiendas

Todas estas son de **2026-10-02, antes de tener los datos de perpetuos y antes de cualquier resultado de selección**.
Para depurar el motor se corrió IS con las matrices viejas de spot (archivos `dev_*`); esas corridas no se usan para
ninguna decisión.

- **E1 · Algoritmo de búsqueda de pesos (precisa §5.2).** Una grilla completa de 0,025 en 7 dimensiones es imposible
  de recorrer (41⁷ combinaciones). Se usa: 1500 direcciones al azar en el símplex (cada estrategia activa con
  probabilidad 0,7, semilla 12345); para cada dirección, la mayor escala que cumple la restricción (bisección); redondeo
  hacia abajo a la grilla de 0,025; descenso por coordenadas de ±0,025 desde las 15 mejores. Durante la búsqueda la
  restricción se evalúa con 200 remuestreos (los mismos para todos los candidatos); el resultado final se verifica con
  2000 (§5.4). Código: `validacion/pesos.py`.
- **E2 · Cartera rápida para la búsqueda.** Durante la búsqueda la cartera se reconstruye cada 4 h con el retorno y la
  **exposición real** (nocional / patrimonio) de cada estrategia corrida sola en el motor, con el reparto en cascada y el
  tope total. No se deduce la posición del retorno (corrige M1). La verificación de §5.4 usa el motor completo.
- **E3 · Corte y alerta por caída apagados en el backtest.** Son resguardos del servicio, no parte de la estrategia;
  incluirlos haría depender los pesos del nivel elegido.
- **E4 · 30 balas.** «Capital asignado al empezar cada campaña» se implementa así: mientras no hay campaña abierta, la
  subcuenta se iguala a peso × patrimonio total (transferencia entre cuentas); durante la campaña no se toca.
- **E5 · Corrida B.** Los símbolos sin contrato hoy en KuCoin (deslistados) usan tamaño 1 unidad y mínimo 1 unidad.
- **E6 · Peor punto de la vela.** Para el rango pesimista, una posición cubierta por completo por su stop se valora en
  el stop (no más allá), porque el stop limita la pérdida dentro de la vela.
- **E7 · Funding de 30 balas.** El motor de balas aplica la tasa vigente de 8 h repartida por vela de 4 h (como en la
  comprobación de paridad), con la serie de XBTUSDM y, antes de 2019-08, la de BTCUSDT.
