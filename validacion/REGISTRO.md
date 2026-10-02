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
