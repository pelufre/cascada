# Cascada 1.2 — respuesta a la revisión y paquete para la segunda auditoría (2026-10-03)

Este paquete trae todo lo necesario para verificar la 1.2 sin confiar en el documento comparativo: el repositorio con
toda su historia (`cascada.bundle`), los datos con huellas SHA-256, las salidas de las pruebas y de las corridas, y los
comandos para reproducir cada cifra. Las respuestas aceptan las objeciones donde corresponde y agregan evidencia nueva
(descomposición de la diferencia en igualdad de condiciones y caída estricta).

## Contenido

| Ruta | Qué es |
|---|---|
| `cascada.bundle` | Repositorio git completo (`git clone cascada.bundle cascada`). Código, protocolo, registro, pesos congelados, resultados y pruebas. Los hashes y fechas de cada commit muestran el orden: protocolo (4a2730d) → datos (8bb03f6) → selección (a273807) → pesos congelados (54ddf76) → evaluación del período de prueba (d8a3e61). También está en GitHub (pelufre/cascada, privado; se puede dar acceso de lectura para cotejar las fechas de push). |
| `datos/perpetuos_4h.zip` | Paquete de perpetuos tal como se recibió (velas 4 h, volumen, funding, listados, contratos KuCoin, `MANIFIESTO.json` con SHA-256 por archivo). Lo verifica `validacion/datos.py` al cargar. |
| `datos/spot_4h/` | Velas spot de Binance: precalentamiento de BTC/ETH/BCH antes de 2020 (enmienda E8) y corrida B de la descomposición. |
| `datos/top50/` | Top 50 semanal de CoinMarketCap y tablas de correspondencia. |
| `datos/funding_XBTUSDM.csv`, `datos/niveles_riesgo_XBTUSDM.csv` | Funding real y tramos de riesgo del inverso de 30 balas. |
| `salidas/` | Salida de las 42 pruebas, selección de pesos, verificación con el motor completo, pesos congelados, resultados del período de prueba por nivel, descomposición y simulación del servicio. |
| `SHA256SUMS.txt` | Huella de cada archivo del paquete y commit HEAD. |

## Respuesta punto por punto

### 0. «Un período que no se usó para elegir nada»
**Aceptado.** La frase correcta es: 2024-01 → 2026-09 no se usó para elegir los pesos ni la decisión de 30 balas. Las
reglas de cada estrategia se diseñaron viendo la historia hasta 2026 (protocolo §1, «Límite reconocido»). Es una
validación temporal de pesos congelados, no una prueba independiente del sistema. Corregido en el documento comparativo.

### 1. Correcciones operativas: pruebas y cobertura
Son 42 pruebas automáticas (eran 29 al escribir el documento; se agregaron la subcuenta de 30 balas y los tramos de
liquidación). Salida completa en `salidas/pruebas.txt`. Los cuatro fallos reproducidos en la auditoría:

| Fallo | Pruebas | Qué exigen |
|---|---|---|
| Llenados parciales | `o01_llenado_parcial`, `o13_kucoin_parcial` | Pide 10, llenan 3: el libro dice 3; en KuCoin se cancela el resto y se informa sólo lo llenado |
| Rechazo del stop | `o02_stop_rechazado` | Si el stop no se puede poner, la posición no queda abierta sin protección |
| Desconciliación | `o05_conciliar_antes`, `o06_stop_cancelado_afuera`, `o07_tope_y_conciliacion` | Con contratos ajenos, ese ciclo sólo reduce; un stop cancelado afuera se repone; tras el tope, libro = exchange |
| Cierre de emergencia fallido | `o11_corte_reintenta` | Si el cierre por corte falla, sigue intentando hasta quedar plano y bloqueado |

Además: los otros once de la auditoría (`o03`–`o15`), red caída antes y después de llenar, reinicio a mitad de ciclo,
velas y precios faltantes, volumen de los perpetuos, capital y funding en papel, tramos de liquidación (`test_servicio`,
11), la subcuenta (`test_subcuenta`, 12) y que los pesos del servicio sean los congelados (`test_config`, 3).

**Límite de estas pruebas:** usan un exchange de prueba programable, no KuCoin. Lo que sólo se prueba contra KuCoin
(órdenes reales, stops, transferencias) tiene comandos guiados con montos mínimos (`cli balas-prueba`,
`cli transferencia-prueba`) que todavía no se corrieron con dinero.

**44 de 44 eventos:** `validacion/simular_servicio.py` corre el servicio completo (`cascada.principal.Sistema`, sin
cambios) ciclo por ciclo sobre las velas del paquete y después el motor de backtest sobre las mismas velas, y compara
cada operación (vela, cuenta, estrategia, símbolo, lado, cantidad, precio). Salida en `salidas/simulacion_servicio.json`.

### 2. M4: caída intrabarra
**Aceptado en parte: la medida «pesimista» del documento sólo resolvía la mitad.** Tomaba el valle en el peor punto
de cada vela, pero el pico sólo en cierres. Se agregó una tercera medida, **estricta**: el pico incluye el mejor punto
de cada vela anterior y de la misma vela, y dentro de una vela se supone que el máximo vino antes que el mínimo (el
orden más desfavorable; con velas de 4 h el orden real no se conoce). Peor punto de una posición: mínimo de la vela
para un largo y máximo para un corto; si un stop cubre toda la posición, el peor punto se limita al stop (enmienda E6).
Definición en `validacion/motor_bt.py`, función `metricas`. La caída verdadera está entre la optimista y la estricta.

| Nivel | 2020–2023: optimista / pesimista / estricta | 2024 → sep 2026: optimista / pesimista / estricta | p95 de 2020–2023 (criterio C2) |
|---|---|---|---|
| 10 | −5,3 / −6,7 / −6,8 % | −4,4 / −4,4 / −6,0 % | 9,4 % |
| 20 | −12,3 / −13,4 / −14,1 % | −8,4 / −9,0 / −9,4 % | 19,2 % |
| 25 | −14,7 / −17,3 / −18,1 % | −10,9 / −11,3 / −11,7 % | 24,2 % |
| 30 | −20,1 / −23,5 / −24,6 % | −13,1 / −14,0 / −14,9 % | 29,6 % |

Con la medida estricta, los cuatro niveles siguen cumpliendo C2. **El p95 no es un límite de pérdida**, de acuerdo: es
el percentil 95 de la caída máxima en 2000 remuestreos de los retornos diarios de 2020–2023, y sirve para calibrar
cuánta caída es esperable. El límite operativo es el corte al 30 %, y tampoco garantiza una pérdida máxima: un hueco de
precio o falta de liquidez puede llevar la caída más allá antes de que el cierre se complete.

### 3. 30 balas: equivalencia con dinero real
**Aceptado.** En papel el capital se iguala a peso × patrimonio al empezar cada campaña, igual que en el backtest. En
real, desde el commit a427369 la transferencia automática entre la cuenta principal y la subcuenta está programada
(`cascada/subcuenta.py`, apagada por defecto) y probada contra un exchange de prueba, pero ni la ejecución de 30 balas ni
la transferencia se corrieron todavía con dinero. Hasta que `balas-prueba` y `transferencia-prueba` salgan bien en
KuCoin, la equivalencia papel ↔ real de ese bloque está pendiente y no se pasa a real.

### 4. Datos de 30 balas
**La inconsistencia es del documento, no del cálculo, y había una segunda que no estaba declarada.**
- Contrato modelado: el inverso XBTUSDM (1 contrato = 1 USD, colateral en BTC, resultado en BTC; `cascada/balas.py`).
- Precio: el perpetuo BTCUSDT de Binance del paquete, porque XBTUSDM sólo tiene velas desde 2024-08 (protocolo §3). La
  diferencia entre el precio del inverso y el del lineal es la base, chica frente a los movimientos que deciden balas.
- Funding: el real de XBTUSDM de KuCoin (`datos/funding_XBTUSDM.csv`), aplicado por vela de 4 h (enmienda E7).
- **Liquidación (no declarado):** el protocolo §4 dice «tramos de riesgo de KuCoin», pero el código usaba un margen de
  mantenimiento fijo de 0,7 %, que es el del tramo 1 (posiciones de hasta 5 BTC). En la corrida B (3000 USDT, la de los
  criterios) la posición de balas no pasó de 0,55 BTC: el tramo 1 es el correcto y los resultados no cambian. En la
  corrida A (100 000 USDT, la de la búsqueda de pesos) llegó a 51 BTC; ahí los tramos reales pedirían entre 1 y 5 % de
  margen y la liquidación estaría más cerca. La corrida A queda como representación de la estrategia sin límite de
  tamaño (todo es proporcional al capital). Desde ahora el servicio aplica los tramos reales (`mmr_xbtusdm`, prueba
  `r11`). **Consecuencia práctica:** el tramo 1 se agota con unos 25 000 USDT de patrimonio total en el nivel 30; por
  encima, 30 balas opera con más margen del validado.

### 5. Papel de 8–12 semanas
**Aceptado.** El papel valida la operación (órdenes, stops, conciliación, recuperación) y la paridad con el motor; por su
duración no puede probar rentabilidad ni el comportamiento en mercados bajistas o squeezes. Corregido en el documento.

### 6. Comparación en el mismo período y atribución de la diferencia
**Aceptado: el documento mezclaba períodos.** Las cifras de la 1.0 recalculadas para 2024-01-01 → 2026-09-30 coinciden
con las de la revisión (52,7 % y 114,8 %). Para atribuir la diferencia se corrió el motor validado cambiando una cosa por
vez, en el mismo período, con 3000 USDT y contratos reales (`validacion/auditoria2.py`, salida en
`salidas/auditoria2_resumen.json`). Es un diagnóstico: evaluar los pesos de la 1.0 en 2024–2026 no cambia nada de lo
congelado (protocolo §8).

| Paso | Qué cambia | Nivel 20: tasa / caída pesimista | Nivel 30: tasa / caída pesimista |
|---|---|---|---|
| A | 1.0 declarada (modelo de investigación, pesos 1.0) | 52,7 % / −13,5 %* | 114,8 % / −21,3 %* |
| B | Motor validado, pesos 1.0, velas spot, con costos, sin funding | 29,2 % / −13,2 % | 46,5 % / −24,8 % |
| C | Igual que B con perpetuos y funding | 37,4 % / −14,7 % | 53,8 % / −33,1 % |
| E | Igual que C sin costos ni funding | 46,7 % / −14,3 % | 77,2 % / −31,4 % |
| D | Motor validado, pesos 1.2 congelados, perpetuos, costos y funding (la evaluación oficial) | 16,3 % / −9,0 % | 28,7 % / −14,0 % |

\* La 1.0 publicó una serie diaria: su caída es sólo de cierres diarios.

Lectura, con la salvedad de que una descomposición secuencial no separa interacciones:
- **A → B, modelo de investigación → motor real (−23 puntos en el nivel 20, −68 en el 30):** es el efecto mayor. El
  modelo de la 1.0 sumaba series diarias por pesos sin posiciones reales, sin el tope sobre las posiciones abiertas, con
  balas rebalanceada a diario y con otra implementación de momentum alts (M1, M2, M6, M7). B también usa el modelo de
  costos de la 1.2, así que este paso mezcla modelo y costos.
- **B → C, spot → perpetuos con funding (+8 y +7 puntos):** los datos de lo que se opera no empeoran el resultado.
- **C → E, costos y funding (9 y 23 puntos por año):** pesan mucho en el nivel 30 de la 1.0, que suma 3,47 veces el
  patrimonio en pesos y rota mucho.
- **C → D, pesos:** los pesos 1.0 del nivel 20 corren con más riesgo que los 1.2 del mismo nombre (caída −14,7 % contra
  −9,0 %). A caída parecida, los pesos 1.0 del nivel 20 dan 37,4 % y los 1.2 del nivel 30 dan 28,7 %. Esos 9 puntos son
  la ventaja de haber elegido los pesos 1.0 viendo 2024–2026: no se puede separar acierto de retrospectiva, y por eso la
  cifra que vale es la de los pesos congelados.
- **Caída estricta:** con los pesos 1.0 la medida estricta se aleja mucho de la pesimista (−26,6 % contra −14,7 % en el
  nivel 20) por el peso de las alts y sus mechas; con los pesos 1.2 la diferencia es de 0,1 a 1,6 puntos.

## Conclusión de nuestra parte
Coincidimos con mantener el papel, no pasar a real y congelar esta versión mientras se verifica. Las condiciones que
quedan antes de real: segunda auditoría de este paquete, `balas-prueba` y `transferencia-prueba` exitosas en KuCoin, y
la fase 3 de papel sin diferencias sin explicar contra el motor.

## Reproducir

```sh
git clone cascada.bundle cascada && cd cascada
pip install -r requirements.txt          # ccxt 4.5.85, pandas 3.0.6, numpy 2.5.3, pyyaml 6.0.3
mkdir -p ../datos/perp && unzip ../datos/perpetuos_4h.zip -d ../datos/perp
T="--top50 ../datos/top50/top50_semanal_2.csv --resumen ../datos/top50/resumen_descarga_1.csv --universo ../datos/top50/universo_1.csv"

# pruebas (42)
for m in test_auditoria test_servicio test_config test_subcuenta; do python -m tests.$m; done
# corridas de cada estrategia sola en 2020–2023 (corrida A) → selección de pesos → verificación con el motor completo
python -m validacion.correr_is --datos ../datos/perp $T --xbt ../datos/funding_XBTUSDM.csv --corrida A
python -m validacion.elegir_pesos
python -m validacion.verificar_motor --datos ../datos/perp $T --xbt ../datos/funding_XBTUSDM.csv
# período de prueba (sólo con pesos_congelados.json commiteado: el cargador se niega sin eso)
CASCADA_ABRIR_OOS=1 python -m validacion.evaluar_oos --datos ../datos/perp $T --xbt ../datos/funding_XBTUSDM.csv
# descomposición y caída estricta
CASCADA_ABRIR_OOS=1 python -m validacion.auditoria2 --perp ../datos/perp --spot ../datos/spot_4h $T --xbt ../datos/funding_XBTUSDM.csv
# servicio completo contra el motor, septiembre 2026
CASCADA_ABRIR_OOS=1 python -m validacion.simular_servicio --perp ../datos/perp $T
```
Nota: `elegir_pesos` y `verificar_motor` vuelven a escribir los archivos de `validacion/resultados/` (comparar con los
commiteados); `evaluar_oos` agrega una línea a `REGISTRO.md`. Tiempos con 2 núcleos: corridas solas ~15 min, selección
~15 min, verificación ~1 h, prueba ~20 min, descomposición ~70 min.
