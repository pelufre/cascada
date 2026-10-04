# Respuesta a la segunda auditoría — Cascada 1.2 → 1.3

Fecha: 2026-10-03. Responde a «Segunda auditoría profunda de Cascada 1.2» (informe y evidencias del 2026-10-03).
Código auditado: c08d856. Correcciones: rama `correcciones-auditoria2` (commit a5719ef y siguientes). Pesos: **los
mismos de 54ddf76**, sin tocar (sha256 5da5dcd4…). Protocolo: enmienda E14 (`PROTOCOLO.md`).

## 1. Resumen

- **Se aceptan los diez hallazgos.** Ocho eran errores de implementación y se corrigieron con pruebas que reproducen
  los casos del informe (V01–V07, V09). V08 describía el tope como más de lo que es: se corrigió la descripción y se
  agregó la medida de exposición económica. V10 es un límite de los datos: se agregó la variante «operable en KuCoin» y
  el análisis de la base XBTUSDM ↔ BTCUSDT, que acotan el punto pero no lo cierran.
- **El script del auditor contra el código nuevo** (`pruebas_independientes.py`, sólo con las llamadas cuya firma
  cambió adaptadas): 8 de sus 9 detecciones ya no se reproducen. La novena comprueba que `Publico.funding("BTC")`
  pide el perpetuo USDT, que es el contrato correcto para los lotes de BTC de la cuenta principal; el defecto era que el
  servicio usara esa llamada para 30 balas, y eso ya no ocurre (balas pide `BTC/USD:BTC`, prueba v07a).
- **Revalidación con el motor corregido y los mismos pesos** (§4): los cuatro niveles siguen cumpliendo C1–C3, también con el p95 congelado y con la caída estricta. Las cifras bajan un poco: la tasa de la prueba baja entre 0,2 y 1,0 puntos (nivel 30: 28,7 % → 27,8 %), la caída pesimista casi no cambia (−14,0 % → −14,2 %) y el p95 de IS sube un poco (29,6 % → 29,8 %). Dos lecturas que agrego: la ventaja contra BTC tendencia no es firme (remuestreo de la prueba: P(tasa del nivel 30 > tasa de BTC tendencia) = 74 %), y BTC al contado rindió en la prueba casi lo mismo que el nivel 30 (28,1 %, con caída de cierres de −53 %).
- **Estado:** pesos y motor validados en el simulador; **equivalencia con dinero real todavía no aprobada.** Faltan
  las pruebas reales con montos mínimos y el papel con la versión 1.3 (§9). La palabra «validada» a secas se retira del
  proyecto: «pesos validados en el simulador; ejecución real pendiente».

## 2. Hallazgo por hallazgo

| | Hallazgo | Qué se hizo | Pruebas | Efecto en los números |
|---|---|---|---|---|
| V01 | Valoración antes de los stops; última vela sin valorar | Stops (con deslizamiento) antes de valorar; posición parada a su llenado real; funding después de los stops; última vela valorada; tramos cerrados en `hasta` | v01a, v01b (los dos ejemplos del informe), v01c | Junto con el resto de E14, la tasa de la prueba baja entre 0,2 y 1,0 pt según el nivel y las caídas cambian ±0,2 pt (§4) |
| V02 | Reserva de balas retroactiva | La reserva se decide al cierre con el precio de ese momento y protege la vela siguiente; nunca la que pasó | v02a, v02b | Nivel 30: 0 reservas en la prueba (el informe contó 0) y 0 en IS; liquidaciones en los cuatro niveles, IS y prueba: 0 |
| V03 | Balas real con contabilidad sombra | En real el estado se lee de la subcuenta tras cada acción; cada acción se anota antes de mandarse; bloqueo ante incertidumbre; se completa o deshace lo que quedó a medias; órdenes con estado terminal; cierre verificado | v03a–v03g, o15b | Sólo real |
| V04 | Órdenes no recuperables en ventanas internas | Aplicación atómica (transacción); recuperación por aplicado=0 en cualquier estado; llenados tardíos de órdenes abiertas; sin aumentos con orden pendiente | v04a–v04d (+ r01–r03) | Sólo ejecución |
| V05 | Dos lados desde plano → un lado | Un lado por símbolo, por prioridad; cambio de lado en dos órdenes | v05a–v05c | Nivel 30: la situación ocurrió 0 veces en IS y 0 en la prueba (incidencia «lado_opuesto»): no mueve los números |
| V06 | Transferencias duplicables | Registro durable, confirmación por saldo, otra ruta sólo tras rechazo explícito, bloqueo de balas si queda incierta | v06a–v06c (+ s06–s12) | Sólo real (apagado por defecto) |
| V07 | Funding y ejecución de balas distintos entre backtest y servicio | Funding de XBTUSDM por evento en los dos; ejecución a la apertura siguiente; comparador con tolerancia de patrimonio | v07a–v07d; simulación del servicio | Funding de 30 balas en la prueba (nivel 30): 138,24 USDT por evento (el informe estimó 138,99 con el método anterior); servicio = motor en septiembre (abajo) |
| V08 | El tope no es un techo de exposición | Descripción corregida (E14.4); se informan nocional total y exposición con el BTC del margen | — | Nivel 30: nocional de futuros máx. 1,012× (IS) y 1,017× (prueba); con el BTC del margen 1,34× y 1,30× |
| V09 | «Sin costos» y atribución incompletos | Costos y funding conmutables también en balas, sin tasa por defecto; atribución con campañas de balas y funding por lote que cierra con el patrimonio | v09, v01c | Residuo de la atribución ~1e-12 USDT; costos y aportes del nivel 30 en §5 |
| V10 | Disponibilidad de contratos y proxies | Variante operable en KuCoin; base XBTUSDM ↔ BTCUSDT; colas del funding | `analisis_v10.py` | Operable en KuCoin, prueba: 8,0 % / 15,4 % / 23,3 % / 26,9 % (entre 0,2 y 0,9 pt menos); en IS más (nivel 30: 59,8 % contra 66,7 %) |

### V01 · Valoración y última vela

Acepto el hallazgo tal como está escrito. La serie se guardaba antes de que el motor ejecutara los stops de la vela: una
posición que ya había salido se marcaba al cierre, y su pérdida o ganancia real aparecía en la fila siguiente. Además, la
enmienda E6 (pre-registrada) topaba el peor punto en el nivel del stop aunque la vela hubiera abierto más allá: era un
error de diseño de la medida, no sólo del código.

Ahora, en cada vela (E14.1): (1) se procesan sus stops; el stop es una orden a mercado y llena con el mismo deslizamiento
que las demás (antes llenaba sin deslizamiento, otra diferencia con la realidad que el informe menciona en Momentum);
(2) se cobra el funding de la vela con la posición que quedó; (3) se valora: cierre con lo que quedó, peor punto con lo
parado a su llenado real, mejor punto con las posiciones del comienzo de la vela. La serie termina en el cierre de la
última vela del tramo (fila t = patrimonio en el instante t) y las métricas incluyen esa fila.

Los dos ejemplos del informe son ahora pruebas de regresión: vela 50/100/50/100 con stop en 90 → patrimonio 500 en esa
misma vela y caída de −50 % en las tres medidas; vela 100/120/80/120 → patrimonio 900 (no 1200), mejor punto 1200 y
caída estricta −25 %. La prueba v01c comprueba que el último patrimonio es el contable (caja + posición al último
cierre) y que la atribución cierra con él.

Qué dice ahora el rango: con la vela completa, el peor punto usa las ejecuciones reales (lo parado, a su llenado; lo
demás, en su extremo desfavorable, todo a la vez) y el mejor punto las posiciones del comienzo de la vela en su extremo
favorable; la caída estricta supone además que el máximo vino antes que el mínimo. Así, la caída verdadera queda entre la
optimista y la estricta sin usar posiciones que ya no existían. No lo afirmo para las velas sin datos (huecos de E9),
donde no hay información dentro de la vela; ni para la liquidación de 30 balas fuera de los datos del proxy (V10).

### V02 · Reserva retroactiva

Acepto. El backtest usaba la apertura de la vela siguiente para decidir la reserva (equivalente a decidirla al cierre
anterior), pero el servicio la decidía al terminar la vela con la apertura de esa misma vela y la mandaba cuando ya había
pasado. Ahora 30 balas trabaja en dos tiempos (E14.2): `vela()` aplica lo que pasó durante la vela con la posición que
había (funding y liquidación con el mínimo) y `decidir()` decide al cierre; la reserva se decide ahí, con el precio de
ese momento, y queda puesta antes de la vela siguiente. El servicio y el backtest llaman a las mismas dos funciones en el
mismo orden. Prueba v02a: con la decisión anterior lejos de la liquidación, la vela 95/100/90/100 liquida (no hay reserva
retroactiva). Prueba v02b: si al decidir el precio ya está a menos de 12 %, la reserva entra y la misma vela no liquida.
No agrego un vigilante intrabarra: el backtest no podría representarlo sin suponer el orden de los precios dentro de la
vela, y la opción elegida es la que el backtest representa exactamente.

### V03 · Balas real

Acepto. En modo real ya no hay estado sombra: después de cada acción y en cada ciclo el modelo se iguala a lo que hay en
la subcuenta (`EjecutorRealBalas.estado()`: USDT y BTC en spot, BTC realizado en futuros, contratos y precio de entrada
de XBTUSDM). Con eso, el patrimonio y el nocional que ve el motor (y el control cada 15 min) son los reales, con los
contratos enteros, las comisiones, el BTC recibido y el funding que cobró el exchange. Cada acción se anota en
`balas_acciones` antes de mandarse; si falla o queda sin respuesta, 30 balas queda bloqueada (no decide) hasta que la
subcuenta esté coherente: si quedó BTC comprado sin llegar a futuros se completa el aporte; si quedó BTC de una campaña
cerrada se vende; recién entonces se levanta el bloqueo, con aviso. Una posición que desaparece sin que la cerremos se
registra como liquidación o cierre externo. Ninguna orden se da por hecha sin estado terminal (si sigue abierta se
cancela el resto y, si aun así no hay estado terminal, `SinConfirmar`), y `cerrar()` comprueba que la posición quedó en
cero. Las pruebas usan una subcuenta falsa con saldos de verdad: apertura que falla antes de hacer nada (v03a), a mitad
de camino (v03b), con la respuesta perdida después de llenar (v03c), patrimonio y nocional reales (v03d), orden sin
estado terminal (v03e), cierre que no queda plano (v03f), transferencia interna con respuesta perdida (v03g).

En el modelo de papel y backtest se agregaron los costos de convertir USDT ↔ BTC en spot (comisión 0,1 % y deslizamiento)
y, en la corrida B, contratos enteros de 1 USD. Los mínimos de spot (0,1 USDT) no se alcanzan con estos tamaños: la
compra más chica del nivel 10 con 3000 USDT es de unos 2,4 USDT.

### V04 · Órdenes principales

Acepto los tres casos. (1) La aplicación de una orden al libro (estado, lotes, operaciones, marcador) es ahora una sola
transacción: si el proceso muere a mitad, no queda nada escrito y la orden sigue con aplicado=0; `_recuperar` busca
todas las órdenes con aplicado=0, en cualquier estado. (2) Una orden que el exchange sigue mostrando abierta nunca se
marca aplicada: se aplica lo llenado y queda pendiente; lo que llene después se suma (con el precio y la comisión del
tramo); la recuperación intenta cancelar el resto. (3) Mientras un símbolo tenga una orden pendiente, sólo se reduce.
Pruebas: v04a (caída después de escribir el estado), v04b (caída entre dos lotes de un mismo reparto), v04c (orden
abierta con 3 de 10 que después llena 10), v04d (no se manda otra orden con una pendiente); r01–r03 siguen en verde.

### V05 · Dos lados desde plano

Acepto. Ahora el lado de un símbolo plano lo decide la entrada de mayor prioridad antes de sumar cantidades; las del
otro lado se omiten (incidencia). Si en el mismo ciclo se cierran los lotes de un lado y entra uno del otro, van en dos
órdenes: primero el cierre, después la entrada. `_ejecutar` rechaza mezclar lados. Pruebas v05a (el caso del informe:
exchange 10 = libro 10), v05b (manda la prioridad aunque pida menos) y v05c (cambio de lado en el mismo ciclo).

### V06 · Transferencias

Acepto. Cada transferencia lógica tiene un id durable (tabla `transferencias`) y cada paso se anota antes de mandarse,
con el saldo que se va a mirar y un clientOid determinado por (transferencia, ruta, paso). Ante un error de red o una
respuesta perdida, el paso no se da por fallido: se mira el saldo de destino hasta confirmar; si no se puede confirmar,
la transferencia queda «incierta», no se prueba otra ruta ni se deshace nada, y 30 balas queda bloqueada. En el ciclo
siguiente `resolver()` decide con los saldos (hecha, no hecha o «revisar» si quedó dinero en una cuenta intermedia). Sólo
un rechazo explícito del exchange hace probar la otra ruta, y la devolución del primer paso de una ruta de dos pasos
también se verifica. Pruebas v06a (el caso del informe: se acreditan 100, se pierde la respuesta → 100, no 200), v06b
(sin respuesta y sin movimiento: incierta, sin otra ruta, y en el ciclo siguiente «fallida») y v06c (el servicio no
transfiere ni abre campaña mientras haya una incierta). La función sigue apagada por defecto.

### V07 · Funding y ejecución de balas

Acepto. (1) El servicio pedía el funding de 30 balas al perpetuo USDT; ahora lo pide a XBTUSDM (`BTC/USD:BTC`) y lo
aplica evento por evento (en papel; en real lo cobra el exchange y se lee). (2) El backtest repartía la tasa de 8 h por
vela; ahora cada evento de XBTUSDM se cobra una vez con la posición de ese momento, después de mirar la liquidación de la
vela (un evento al cierre de una vela que liquidó no se cobra). (3) Balas ejecutaba al cierre; ahora a la apertura
siguiente, como la cuenta principal. (4) En la cuenta principal el funding se cobra después de los stops (prueba v07c).
(5) Se registra el momento real de envío y el precio de referencia de cada orden (latencia). (6) La simulación del
servicio y `comparar_papel` usan el funding de XBTUSDM en los dos lados, comparan balances (caja, capital de balas,
funding de cada cuenta) y fallan también si el patrimonio se separa más de 0,5 %.

Simulación del servicio completo, septiembre 2026, con el código 1.3: 44 eventos (operaciones 44 en el servicio y 44 en el motor) y 0 diferencias; patrimonio final 3.106,34 (servicio) y 3.106,34 (motor); diferencia máxima en un cierre de 4 h 0,063 % (tolerancia 0,5 %); funding de 30 balas 2,7059 USDT en los dos lados (XBTUSDM) y de la principal 2,7337 en los dos; capital de 30 balas 1.083,34 y 1.083,34. Antes el servicio usaba funding constante para balas y el comparador una tasa sintética; ahora los dos usan XBTUSDM.

### V08 · El tope

Acepto la observación. El tope de §4 limita el nocional de futuros (principal + el de 30 balas), sólo recorta la cuenta
principal y tiene una holgura de 5 %: lo nuevo se asigna hasta 1× y lo abierto se recorta recién por encima de 1,05×,
para no operar con cada movimiento de precio. No es un techo rígido de 1×, ni de exposición económica, ni de pérdida.
Eso queda escrito (E14.4) y se informan dos medidas en cada corrida: nocional de futuros / patrimonio y exposición
económica / patrimonio (nocional de la principal + delta en USD de 30 balas, que incluye el BTC del margen).
En el nivel 30 (corrida B) el nocional de futuros llegó a 1,012× del patrimonio en IS y 1,017× en la prueba (dentro de la holgura), y la exposición económica con el BTC del margen a 1,34× y 1,30×. No cambio la regla: hacerlo después de abrir la prueba sería elegir una configuración mirándola.

### V09 · Costos y atribución

Acepto. «Sin costos / sin funding» ahora apaga también las comisiones, el deslizamiento y el funding de 30 balas, y
30 balas ya no inventa una tasa de 10 % anual cuando no recibe datos. La atribución (`info["atribucion"]` de cada
corrida) suma los lotes de la principal (cerrados y abiertos al último cierre, netos de comisiones y del funding que pagó
cada lote) y las campañas de 30 balas (cerradas y la abierta), y cierra con el patrimonio: el residuo se informa y es del
orden de 1e-12 USDT. Factor de beneficio, ganadoras y peor lote se calculan con los lotes cerrados netos de funding; las
campañas de balas se informan aparte. La descomposición 1.0 → 1.3 se rehízo con el «sin costos» verdadero (§6).

### V10 · Datos

Acepto que las huellas prueban identidad, no exactitud, y que E5 trataba como deslistados símbolos que KuCoin nunca
tuvo (DEXE, IP, JST) o tiene con otro ticker (BONK como 1000BONK). Tres agregados:

1. **Variante «operable en KuCoin»** (informativa): sólo símbolos con contrato USDT-M del mismo ticker (como los ve el
   servicio, que no traduce 1000BONK → BONK) y desde su fecha de apertura en KuCoin. En la prueba, el nivel 30 da 26,9 % con caída −13,8 % (contra 27,8 % y −14,2 %); según el nivel, la tasa baja entre 0,2 y 0,9 pt. En IS la diferencia es mayor (nivel 30: 59,8 % contra 66,7 %) porque en 2020–21 KuCoin listaba muchos menos contratos (el de BTC USDT-M abrió el 2020-03-30). Los cuatro niveles operables también cumplen C1–C3 en la prueba con el p95 de su propio IS. El catálogo es
   el actual: los contratos que KuCoin tuvo y deslistó (FTM) quedan afuera, así que la variante es conservadora en ese
   sentido. Un catálogo histórico con fechas de alta y baja sigue pendiente (requiere datos de KuCoin que no tengo).
2. **Base XBTUSDM ↔ BTCUSDT** (`analisis_v10.json`): el archivo de velas de XBTUSDM tiene huecos: 1.877 velas de 4 h completas entre 2024-08 y 2026-09 (40 % del tramo). Donde hay datos, la base contra el proxy es chica: cierre mediana 0,02 %, p1/p99 −0,18 % / 0,17 %, máximo 0,55 %; mínimos p1 −0,15 %, con una mecha de −4,07 % en XBTUSDM el 2025-11-21 04:00. Con esas velas, 30 balas sola (1000 USD, corrida B) hace 72 campañas contra 74, 0 liquidaciones y 0 reservas en los dos casos, resultado 2.018,40 contra 2.036,11 (−0,9 %) y distancia mínima a la liquidación 18,8 % contra 18,8 %: la mecha no se acercó a la liquidación. No es una prueba completa: falta el resto de las velas, y antes de 2024-08 no hay velas de XBTUSDM.
3. **Colas del funding:** 582 eventos del paquete con |tasa| > 1 % y 2.024 con |tasa| > 0,5 %; el mínimo, −4,65 %, es CRO el 2022-11-13 12:00 (intervalo 8 h, la semana de la caída de FTX) y el máximo, 1,24 %, HNT el 2023-03-20 16:00. No los toco: son las tasas publicadas para esos contratos; quedan listados con símbolo, fecha e intervalo en `analisis_v10.json`.
4. **Exposición de la cartera a esos supuestos** (lotes del nivel 30, corrida B, `exposicion_v10.json`):
   lotes en símbolos sin contrato del mismo ticker en el catálogo actual de KuCoin: 29 (IS y prueba), resultado 1.607,11 USDT netos de comisiones. En la prueba: DEXE 291,49 (2 lotes), IP 3,14 (1 lote), BONK −1,71 (1 lote), FTM −1,22 (2 lotes), JST −0,03 (1 lote) (en KuCoin: BONK como 1000BONK, FTM hoy S). En IS dominan FTT 762,66, EOS 296,61 (hoy A), FTM 240,84 (hoy S), MKR 53,20, MATIC −49,77 (hoy POL), HNT 29,65: muchos son cambios de ticker o deslistados (FTT), no símbolos que nunca se pudieron operar; por eso la variante operable es una cota conservadora, sobre todo en IS. Huecos de 2022 (E9): 4 lotes abiertos durante un hueco; el mayor, un corto de TRX de 796 USDT que reapareció 4,0 % en contra (≈ −32 USDT); el peor movimiento fue −10,2 % en WAVES (43 USDT, salió por stop al reaparecer). Funding extremo (|tasa| ≥ 1 %): 59 eventos sobre lotes abiertos, ≈ 520 USDT pagados en total (261 en el corto de FTT durante la caída de FTX); están incluidos en los resultados.

## 3. Sección 5 del informe (estrategias)

Acepto las precisiones y quedaron escritas en el protocolo (E14.8): ab_cortos y los RSI2 no tienen stop por lote; la
volatilidad objetivo de WR2 fija el tamaño al entrar; el riesgo de Soldados es del presupuesto de la estrategia; «5x» es
el mecanismo dentro de cada campaña; la decisión §5.5 comparó dos carteras optimizadas, no dos estrategias. Las cifras
por estrategia del informe salían de lotes de la cuenta principal sin balas ni funding; ahora la atribución las incluye
(§5).

## 4. Revalidación con el motor 1.3 y los mismos pesos (E14)

**Corrida B (3000 USDT, contratos reales). Antes = motor 1.2 (c08d856); después = motor 1.3.**

| Nivel | Tasa IS antes → después | p95 IS antes → después | Tasa prueba antes → después | Caída pesimista prueba antes → después | Caída estricta prueba antes → después | Calmar prueba antes → después | Sharpe prueba | C1 | C2 | C3 |
|---|---|---|---|---|---|---|---|---|---|---|
| 10 % | 16,5 % → 16,2 % | 9,4 % → 9,6 % | 8,4 % → 8,2 % | −4,4 % → −4,5 % | −6,0 % → −6,0 % | 1,90 → 1,83 | 1,41 | ✔ | ✔ | ✔ |
| 20 % | 37,4 % → 36,5 % | 19,2 % → 19,5 % | 16,3 % → 15,7 % | −9,0 % → −8,8 % | −9,4 % → −9,2 % | 1,81 → 1,79 | 1,28 | ✔ | ✔ | ✔ |
| 25 % | 50,6 % → 49,6 % | 24,2 % → 24,5 % | 24,5 % → 23,8 % | −11,3 % → −11,3 % | −11,7 % → −11,8 % | 2,18 → 2,11 | 1,42 | ✔ | ✔ | ✔ |
| 30 % | 68,6 % → 66,7 % | 29,6 % → 29,8 % | 28,7 % → 27,8 % | −14,0 % → −14,2 % | −14,9 % → −14,9 % | 2,05 → 1,96 | 1,31 | ✔ | ✔ | ✔ |

C2 compara la caída pesimista de la prueba con el p95 de IS del mismo motor. También se cumple con el p95 congelado y con la caída estricta en los cuatro niveles.

**Por año** (prueba con arranque nuevo en 2024; IS 2020–2023) y exposición máxima:

| Nivel | Prueba 2024 antes → después | 2025 antes → después | 2026 (a sep) antes → después | IS 2020 | 2021 | 2022 | 2023 | Nocional máx. / patrimonio (IS · prueba) | Exposición máx. con BTC del margen (IS · prueba) |
|---|---|---|---|---|---|---|---|---|---|
| 10 % | 9,3 % → 9,1 % | 8,7 % → 8,3 % | 5,2 % → 5,3 % | 23,4 % | 23,1 % | 7,6 % | 11,5 % | 0,277× · 0,244× | 0,33× · 0,29× |
| 20 % | 19,7 % → 18,5 % | 18,2 % → 17,7 % | 7,1 % → 7,1 % | 45,2 % | 57,9 % | 12,2 % | 34,8 % | 0,661× · 0,626× | 0,87× · 0,80× |
| 25 % | 34,6 % → 33,7 % | 20,7 % → 19,7 % | 12,4 % → 12,4 % | 69,5 % | 72,1 % | 14,7 % | 49,8 % | 0,879× · 0,783× | 1,11× · 0,98× |
| 30 % | 41,9 % → 40,1 % | 28,1 % → 27,3 % | 10,0 % → 10,0 % | 86,2 % | 99,8 % | 23,8 % | 67,7 % | 1,012× · 1,017× | 1,34× · 1,30× |

| Nivel | Corrida A IS: tasa · p95 | Corrida A prueba: tasa · caída pesimista · estricta |
|---|---|---|
| 10 % | 16,9 % · 9,9 % | 9,2 % · −4,8 % · −6,1 % |
| 20 % | 37,4 % · 20,2 % | 16,4 % · −9,8 % · −10,2 % |
| 25 % | 50,8 % · 25,1 % | 24,2 % · −12,2 % · −12,7 % |
| 30 % | 67,5 % · 30,3 % | 27,7 % · −15,0 % · −15,6 % |

**Referencias en la prueba (corrida B)**

| Caso | Tasa | Caída | Calmar |
|---|---|---|---|
| BTC tendencia (SMA 200, perpetuo 1×) | 16,3 % | −35,6 % | 0,46 |
| BTC mantener (perpetuo 1×, funding) | 19,0 % | −56,1 % | 0,34 |
| ETH mantener (perpetuo 1×, funding) | −2,8 % | −71,2 % | −0,04 |
| Pesos iguales por riesgo (nivel 30) | 20,0 % | −11,6 % | 1,73 |
| Nivel 30 sin 30 balas | 14,6 % | −9,7 % | 1,50 |
| Nivel 30 sin alts | 19,2 % | −15,1 % | 1,27 |
| BTC al contado (comprar y mantener) | 28,1 % | −53,4 % | 0,52 |
| ETH al contado (comprar y mantener) | 6,0 % | −68,0 % | 0,09 |

**Incertidumbre muestral de la ventaja contra BTC tendencia** (2000 remuestreos por bloques de los retornos diarios de la prueba, pareados; percentiles 5 / 50 / 95 de la tasa anual):

| Nivel | Cartera | BTC tendencia | Diferencia | P(diferencia > 0) |
|---|---|---|---|---|
| 10 % | 3 % / 8 % / 14 % | −20 % / 14 % / 69 % | −56 % / −7 % / 25 % | 38 % |
| 20 % | 5 % / 15 % / 27 % | −20 % / 14 % / 69 % | −45 % / 0 % / 31 % | 51 % |
| 25 % | 8 % / 23 % / 43 % | −20 % / 14 % / 69 % | −31 % / 8 % / 34 % | 67 % |
| 30 % | 9 % / 27 % / 51 % | −20 % / 14 % / 69 % | −25 % / 12 % / 40 % | 74 % |

**Variante operable en KuCoin** (informativa; símbolos con contrato del mismo ticker, desde su apertura):

| Nivel | IS: tasa · caída · p95 | Prueba: tasa · caída · Calmar |
|---|---|---|
| 10 % | 14,9 % · −5,9 % · 8,7 % | 8,0 % · −4,5 % · 1,78 |
| 20 % | 31,9 % · −13,2 % · 18,2 % | 15,4 % · −8,6 % · 1,79 |
| 25 % | 44,4 % · −16,7 % · 22,9 % | 23,3 % · −11,2 % · 2,08 |
| 30 % | 59,8 % · −23,1 % · 28,6 % | 26,9 % · −13,8 % · 1,94 |

Corrida A (capital grande, contratos fraccionarios): el p95 de IS del nivel 30 da 30,3 % y el del nivel 20, 20,2 %, apenas por encima del nivel que se usó al elegir los pesos (con el motor 1.2: 29,98 % y 19,9 %). No es un criterio (C2 se mide en la corrida B) y los pesos no se reescalan: están congelados (§8). Al contado: caída de cierres de 4 h. Calmar con la caída estricta en la prueba: 10 % 1,37 / 20 % 1,72 / 25 % 2,03 / 30 % 1,86 (BTC tendencia 0,44).

## 5. Atribución y costos del nivel 30 en la prueba (corrida B)

| Parte | USDT | Nota |
|---|---|---|
| balas5 | 1.200,09 | 111 campañas, 0 liquidaciones; funding 138,24, comisiones 192,60 |
| ab_cortos | 766,48 |  |
| rsi2_eth | 501,35 | incluye 228,29 de lotes abiertos al último cierre |
| rsi2_btc | 276,50 | incluye 93,41 de lotes abiertos al último cierre |
| mom_alts | 77,70 | incluye 70,62 de lotes abiertos al último cierre |
| sold_btc | 62,46 |  |
| **Total** | **2.884,58** | = patrimonio final − inicial; residuo de la principal −5e-12, de balas −2e-12 |

Costos de la prueba (nivel 30, B): funding de la cuenta principal 288,50 USDT y de 30 balas 138,24; comisiones de la principal 83,11 y de 30 balas 192,60 (incluye la conversión USDT ↔ BTC). Lotes cerrados de la principal, netos de funding: 281, 35 % ganadores, factor de beneficio 1,67; campañas de 30 balas: 111, 48 % ganadoras, factor 2,53, peor −197,22 USDT.

## 6. Descomposición 1.0 → 1.3, rehecha

Mismo período (2024-01-01 → 2026-09-30), corrida B. Tasa anual y caída pesimista; motor 1.2 → motor 1.3.

| Paso | Qué cambia | Nivel 20: tasa 1.2 → 1.3 | caída 1.3 | Nivel 30: tasa 1.2 → 1.3 | caída 1.3 |
|---|---|---|---|---|---|
| A | 1.0 declarada (investigación, pesos 1.0, spot) | 52,7 % → 52,7 % | −13,5 % | 114,8 % → 114,8 % | −21,3 % |
| B | motor, pesos 1.0, spot, sin funding, con costos | 29,2 % → 29,3 % | −13,1 % | 46,5 % → 47,0 % | −25,0 % |
| C | motor, pesos 1.0, perpetuos + funding, con costos | 37,4 % → 36,8 % | −14,8 % | 53,8 % → 52,9 % | −33,1 % |
| E | motor, pesos 1.0, perpetuos, sin costos ni funding (1.2: balas conservaba sus costos) | 46,7 % → 48,9 % | −14,1 % | 77,2 % → 83,7 % | −30,5 % |
| D | motor, pesos congelados (la evaluación oficial) | 16,3 % → 15,7 % | −8,8 % | 28,7 % → 27,8 % | −14,2 % |

A: la serie diaria que declaraba la 1.0 (caída de cierres diarios); el resto, caída pesimista del motor 1.3. Costos del paso C en la prueba (arranque 3000 USDT): nivel 20, comisiones 261 y funding 685 USDT; nivel 30, 740 y 1.327. E − C no es «el costo»: sin costos ni funding cambian también las señales, los tamaños y la trayectoria; los pasos describen, no se suman.

## 7. Sección 6 del informe (estadística y protocolo)

- **El OOS no es una muestra virgen para las ideas.** De acuerdo; ya estaba en §1 del protocolo. La única muestra limpia
  para las ideas es la que se acumule desde ahora, en papel y después en real.
- **El p95 sale de retornos diarios de cierre** y no incluye mínimos intrabarra ni incertidumbre de estimación; 2000
  remuestreos no son 2000 historias. De acuerdo. Comparar la caída estricta con ese p95 es un criterio operativo
  declarado, no una cobertura al 95 %. Los niveles son etiquetas (E14.8).
- **C1–C3 son débiles para certificar robustez.** De acuerdo. Se agrega, informativo, un intervalo por remuestreo de la
  ventaja contra BTC tendencia en la prueba (§4). No corrige el sesgo de selección de las ideas.
- **Benchmark al contado.** Agregado: comprar y mantener BTC y ETH al contado, sin funding (§4).
- **E12–E14 después de abrir la prueba.** Documentadas como correcciones sin cambio de pesos (§8 del protocolo); las
  series de cada versión del motor quedan identificadas («motor 1.2» en `resultados/`, «motor 1.3» en
  `resultados/revalidacion/`).
- **Custodia del holdout.** `autorizado_oos` exige ahora además la huella SHA-256 del archivo congelado, y el modo PAPEL
  del motor sólo funciona con un cargador de datos del servicio. Sigue siendo una barrera de flujo dentro del repositorio,
  no una custodia externa: las fechas de Git no prueban por sí solas cuándo se decidió algo.

## 8. Diferencia 1.0 → 1.3

Mantengo la lectura del informe: el ejercicio es descriptivo, los pasos no son aportes causales que se sumen, y el caso E
anterior conservaba costos de balas. La versión rehecha está en §6.

## 9. Qué falta para aprobar dinero real (y no está hecho)

1. **Pruebas reales con montos mínimos** en la subcuenta: `cli balas-prueba 15` (ahora muestra también el estado que lee
   el sistema) y, si se activa, `cli transferencia-prueba 15`. Pendiente del lado del usuario (passphrase de la subcuenta).
2. **Papel con la versión 1.3** durante 8–12 semanas, con `comparar_papel` semanal (eventos y patrimonio). Sirve para
   comprobar ejecución y recuperación, no para demostrar rentabilidad.
3. **Escenarios de falla en real** que las pruebas cubren con dobles (respuesta perdida, llenado tardío, reinicio en cada
   paso): repetirlos con montos mínimos donde se pueda provocar sin riesgo (por ejemplo, cortar el proceso entre la compra
   de BTC y los contratos y ver que el ciclo siguiente lo deshaga).
4. **Catálogo histórico de KuCoin** (altas y bajas de contratos) para cerrar V10.
5. Capacidad: con el tramo 1 de XBTUSDM, hasta ~25 000 USDT de patrimonio total en el nivel 30 (E12).
6. La conciliación de patrimonio y cargos contra el exchange real (no contra el papel) sólo puede hacerse con dinero
   real; las pruebas de esta respuesta la hacen contra dobles con saldos y contra el servicio en papel.

## 10. Reproducción

```
git clone cascada.bundle cascada && cd cascada && git pull ../cascada_desde_c08d856.bundle correcciones-auditoria2
for m in test_auditoria test_servicio test_config test_subcuenta test_auditoria2; do python -m tests.$m; done
export CASCADA_ABRIR_OOS=1; T="--top50 … --resumen … --universo … --xbt ../datos/funding_XBTUSDM.csv"
python -m validacion.revalidar --datos ../datos/perp $T                 # IS y prueba, A y B, comparaciones, operable
python -m validacion.auditoria3 --perp ../datos/perp --spot ../datos/spot_4h $T   # descomposición y lotes
python -m validacion.analisis_v10 --datos ../datos/perp --xbt_velas velas_XBTUSDM_60m.csv --xbt ../datos/funding_XBTUSDM.csv
python -m validacion.simular_servicio --perp ../datos/perp $T --desde 2026-09-01 --hasta 2026-10-01
```
