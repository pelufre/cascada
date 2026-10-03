# Registro de la validación

| Fecha | Qué | Commit / hash |
|---|---|---|
| 2026-10-02 | Protocolo pre-registrado (antes de cualquier corrida nueva) | este commit |
| 2026-10-02 | Enmiendas E1–E7 (motor y búsqueda), antes de tener datos de perpetuos | 6153258 |
| 2026-10-02 | Paquete de perpetuos recibido (perpetuos_4h.zip, sha256 a4760b49afac4508…); enmiendas E8–E9 antes de correr | 8bb03f6 |
| 2026-10-02 | Arreglo del motor de backtest: velas faltantes (E9) dejaban funding y caja en NaN; se valora con el último cierre. Sólo IS | este commit |
| 2026-10-02 | Corridas IS de cada estrategia sola (corrida A, 2020-01-01 → 2023-12-31) | este commit |
| 2026-10-02 | Arreglo de `pesos.verificar`: al escalar por 0,99 redondeaba hacia abajo a la grilla y cada peso perdía un paso de 0,025; ahora escala exacta (§5.4). Se descarta la corrida de selección interrumpida y se repite completa | este commit |
| 2026-10-02 | Selección de pesos con IS (`elegir_pesos`, 1500 direcciones): 30 balas se queda (55,3 % vs 46,2 % en el nivel 20 %); pesos por nivel en resultados/seleccion_is.json | este commit |
| 2026-10-02 | Observación en la verificación §5.4 (sólo IS): el motor completo da más exposición y caída que la cartera rápida (nivel 20 %: p95 32,7 % vs 19,9 %). Causa: los lotes fijos (mom_alts, ab_cortos, Soldados) no se achican, y con peso chico un ganador crece más contra el patrimonio total que en la corrida sola (mom_alts llega a 58 % de exposición con peso 0,141). No es un error; se aplica el remedio pre-registrado de §5.4 (escala común hasta cumplir con el motor completo). El método no se cambia | este commit |
| 2026-10-02 | Enmienda E10 (cartera por lotes). Corridas solas repetidas con registro por lote (mismos resultados). La selección con E2 se descarta y se guarda en resultados/e2_cartera_rapida/ | este commit |
| 2026-10-02 | Selección de pesos IS con E10 (1500 direcciones): 30 balas se queda (41,6 % vs 35,4 %); niveles 10/20/25/30 → 17,1/38,1/51,7/68,4 % anual IS | este commit |
| 2026-10-02 | Verificación §5.4 con el motor completo (A y B): los cuatro niveles cumplen con factor 1 (B: 16,5/37,4/50,6/68,6 % anual IS; p95 9,4/19,2/24,2/29,6 %) | este commit |
| 2026-10-02 | Nivel elegido por el usuario: 30 %. Pesos congelados (pesos_congelados.json sha256 5da5dcd4398b996a…) | este commit |
| 2026-10-02 | Evaluación OOS (pesos 54ddf76): APROBADA | resultado_oos.json sha256 5066df9d90a0bc42 |
| 2026-10-02 | Evaluación OOS informativa nivel 10 (pesos 54ddf76): APROBADA | resultado_oos_nivel10.json sha256 6ab39ebff3cdb0f4 |
| 2026-10-02 | Evaluación OOS informativa nivel 20 (pesos 54ddf76): APROBADA | resultado_oos_nivel20.json sha256 ba8909ca3e75d3dd |
| 2026-10-02 | Evaluación OOS informativa nivel 25 (pesos 54ddf76): APROBADA | resultado_oos_nivel25.json sha256 8d57b488748d42e4 |
| 2026-10-02 | Servicio 1.2.0 alineado con lo validado: niveles congelados (nivel 30 por defecto, Soldados activo), tope con el nocional real de balas, capital de balas por campaña (E4) y funding en papel, deslizamiento del papel como el backtest, liquidez de c40 con volumen del perpetuo, alerta 20 % / corte 30 %. Comparación papel ↔ motor (comparar_papel.py) | este commit |
| 2026-10-03 | Real: reequilibrio automático de la subcuenta de 30 balas entre campañas (E4 en real), apagado por defecto (balas_transferir) hasta probarlo con cli transferencia-prueba | este commit |
