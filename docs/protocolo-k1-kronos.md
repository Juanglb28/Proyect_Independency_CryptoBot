# Protocolo pre-registrado — K1: backtest histórico de Kronos como señal competidora (enmienda E6) — v1

- **v1** — 2026-10-15, redactado tras cerrar K0 y **antes de generar ningún pronóstico de K1**. **Aprobado por el usuario el 2026-10-15 y congelado.** Se versiona en Git; el generador calcula la huella SHA-256 de este archivo (con saltos de línea normalizados), la guarda con cada pronóstico y en `data/kronos/k1_fechas.json`, y se niega a continuar si el texto cambia.

Cualquier cambio posterior a este texto es un experimento NUEVO, no un ajuste de este. Es un estudio offline de **sombra**: no modifica el score, el prompt, el pre-filtro, las compras ni ninguna regla de riesgo (E6).

## Pregunta

¿El retorno esperado que pronostica Kronos-small para las próximas 24 velas de 1 h ordena las monedas, dentro de cada fecha, de acuerdo con el desenlace simulado de entrar a ese precio con nuestras reglas de salida? ¿Y lo hace mejor que dos reglas simples de momentum?

## Lo que K0 dejó medido (hechos, no resultados de rendimiento)

Carga sin red desde pesos fijados; reproducible con la misma semilla; 16,1 s por pronóstico de 8 trayectorias en la CPU del usuario; correlación de rangos del retorno esperado entre dos muestreos independientes de la misma fecha = 0,94 (error de muestreo ≈ 13 % de la dispersión entre monedas). Esto justifica 8 trayectorias; no dice nada sobre capacidad predictiva.

## Parámetros congelados del modelo

| Parámetro | Valor |
|---|---|
| Modelo / tokenizador | `NeoQuasar/Kronos-small` @ `901c26c133…` / `NeoQuasar/Kronos-Tokenizer-base` @ `0e0117387f…` (las revisiones completas y los SHA-256 salen de `data/kronos/pesos.lock.json`, versionado) |
| Código de Kronos | commit `67b630e67f6a18c9e9be918d9b4337c960db1e9a` |
| Lookback / horizonte | 400 velas de 1 h / 24 velas de 1 h |
| Trayectorias | 8 por pronóstico |
| Temperatura / top_p | 1,0 / 0,9 |
| Semilla base | 20261015; la semilla de cada pronóstico se deriva de (semilla base, símbolo, fecha) con `semilla_de_contexto` |
| Entradas | solo OHLC (sin volumen) |
| Dispositivo | CPU |

## Muestra

- **Universo:** los símbolos de la lista autorizada (`ALLOWED_SYMBOLS`); ninguno se añade.
- **Fechas de decisión (primaria):** 00:00 UTC de 2024-07-01 y cada 3 días a partir de ahí (2024-07-01, 07-04, …), hasta la última fecha cuyo contexto (400 velas cerradas hasta las 23:00 del día anterior) exista en la base al iniciar la generación. La lista de fechas se calcula una vez, se guarda y no cambia. Estimación a la fecha de este protocolo: ~279 fechas × 20 monedas ≈ 5.580 pronósticos ≈ 25 h de CPU.
- **Corte de contaminación:** Kronos se preentrenó con datos de Binance hasta junio de 2024, por eso ninguna fecha anterior a 2024-07-01 entra al análisis primario.
- **Sensibilidad (secundaria):** el mismo análisis restringido a fechas ≥ 2025-03-19 (frontera de la época no vista del score). Es un subconjunto de la muestra primaria: no genera pronósticos nuevos.
- **Diagnóstico de contaminación (secundario, solo informativo):** pronósticos para cada 15 días entre 2023-07-01 y 2024-06-30 (≈24 fechas × 20 monedas ≈ 2 h), si la base tiene velas suficientes. Sirve para ver si Kronos rinde mucho mejor sobre datos que pudo ver en el preentrenamiento. Nunca se usa como evidencia de capacidad.
- **Datos faltantes:** un contexto con hueco, velas insuficientes, vela no cerrada o precio no positivo no se pronostica ni se rellena; se cuenta por motivo (P3).

## Señal y desenlace

- **Señal primaria:** `retorno_esperado` = media sobre las 8 trayectorias de `cierre[t+24] / cierre[t] − 1`, donde `t` es la última vela de 1 h cerrada antes de las 00:00 UTC de la fecha de decisión. Secundaria (informativa): fracción de trayectorias al alza.
- **Desenlace:** exactamente el del arnés del backtest del score (`observaciones_de_simbolo`): entrada al cierre de la vela de 1 h de las 23:00 del día anterior (precio a las 00:00 UTC) y `simular_salida` con las reglas de salida del motor paper (stop −8 %, cierre parcial del 50 % en +12 %, trailing del 6 %, comisión 0,1 % por lado). El desenlace es el `retorno_neto`. Las observaciones que siguen abiertas al final de los datos quedan **censuradas**: no entran y se cuentan.
- **Sin look-ahead:** el contexto de cada pronóstico solo contiene velas cerradas hasta la hora de decisión (lo comprueba la prueba `test_las_velas_posteriores_a_as_of_nunca_entran…`); el desenlace se lee después y no entra al pronóstico.

## Contraste primario (único)

**RankIC diario:** para cada fecha, correlación de Spearman entre la señal primaria y el `retorno_neto` de las monedas con desenlace resuelto en esa fecha. Solo cuentan las fechas con ≥ 10 monedas resueltas (el resto se excluye y se cuenta). Estadístico = promedio de los RankIC diarios.

**Intervalo:** bootstrap **por bloques de fechas** (se remuestrean bloques de 5 fechas consecutivas muestreadas, es decir, 15 días, con reemplazo, hasta completar el número de fechas), 1.000 iteraciones, semilla 20261005, intervalo percentil de 95 % (percentiles 2,5 y 97,5). Los bloques recogen que los desenlaces de fechas vecinas se solapan en el tiempo.

## Contrastes secundarios (exploratorios; no deciden)

1. **Terciles de §8.7:** diferencia entre el tercil superior e inferior de la señal sobre todas las observaciones resueltas de la muestra primaria, con el mismo método de §8.7 (1.000 iteraciones, semilla 20261005), usando `retorno_neto`.
2. **Versión E4:** el mismo contraste con el `retorno_neto` centrado por mes calendario UTC de la fecha de decisión.
3. **Sensibilidad ≥ 2025-03-19** y **diagnóstico pre-corte** (arriba).
4. **Prueba de permutación:** 1.000 barajados de la señal entre monedas dentro de cada fecha (semilla 20261005); p-valor de una cola = fracción de barajados con RankIC promedio ≥ el observado.
5. **Correlación de rangos Kronos ↔ score v1:** promedio de los Spearman diarios entre ambas señales (si es alta, Kronos no aporta información nueva).

## Líneas base (calculadas sobre las mismas observaciones)

- **Score cuantitativo v1** (el del backtest walk-forward), solo en las fechas donde se puede calcular.
- **Momentum 24 h:** `cierre[t] / cierre[t−24 h] − 1`.
- **Momentum 7 d:** `cierre[t] / cierre[t−168 h] − 1`.
- **Permutación:** ver arriba.

Con cada una se calcula el mismo RankIC diario sobre las mismas fechas y monedas.

## Regla de decisión (fija)

Estado de la muestra: **NO_CONCLUYENTE_MUESTRA_INSUFICIENTE** si hay menos de 30 fechas con ≥ 10 monedas resueltas. Si no:

- **NO_DISCRIMINA:** el límite inferior del IC95 del RankIC promedio de Kronos es ≤ 0.
- **DISCRIMINA_NO_AÑADE:** el límite inferior es > 0, pero la diferencia pareada de RankIC entre Kronos y **cada una** de las líneas base de momentum (24 h y 7 d) tiene límite inferior ≤ 0 en al menos una. La diferencia pareada se calcula con el mismo bootstrap de bloques (mismas fechas remuestreadas para ambas señales). Se compara contra las dos y no contra «la mejor», para no elegir a posteriori.
- **DISCRIMINA_Y_AÑADE:** el límite inferior es > 0 y la diferencia pareada frente a ambos momentum tiene límite inferior > 0.

**Alcance del veredicto.** Incluso `DISCRIMINA_Y_AÑADE` solo justifica pasar a K2 (sombra hacia delante, ≥ 30 días, sin cambiar ninguna decisión). No incorpora la señal al score, al pre-filtro ni a las compras: eso requeriría otra enmienda y otro protocolo con datos no vistos. Cualquier resultado se reporta completo, incluyendo los secundarios que no favorezcan a Kronos.

## Prohibido tras ver resultados

Cambiar el número de trayectorias, la temperatura, top_p, la semilla, el lookback, el horizonte, el modelo, el espaciado de fechas, el corte de 2024-07-01, el tamaño de bloque, el mínimo de 30 fechas o de 10 monedas, la señal primaria, el desenlace o la regla de decisión. Cualquier variante es un experimento nuevo con su propia corrección y, si usa los mismos datos, debe declararse como tal.

## Operación (sin efecto estadístico)

- La generación corre en el contenedor aislado de Kronos y guarda cada pronóstico en la tabla nueva `kronos_forecasts` (migración propia; sin claves foráneas hacia tablas operativas; nunca escribe en `propuestas`, `ordenes` ni `posiciones`). Clave única: (símbolo, fecha, revisión de pesos, lookback, horizonte, trayectorias, semilla base). Reintentar no duplica.
- Es **reanudable**: se puede detener y continuar; el orden de las fechas es determinista. Cada fila guarda versiones de código, pesos, parámetros, dispositivo, huella de este protocolo y las 8 cifras finales, para poder recalcular cualquier resumen.
- La evaluación (RankIC, bootstrap, líneas base) lee esa tabla y las velas en solo lectura y no necesita el modelo.

## Limitaciones declaradas de antemano

- ~20 monedas muy correlacionadas: la muestra efectiva es mucho menor que el número de observaciones; el bootstrap por bloques lo mitiga, no lo elimina, y el IC puede ser optimista.
- Los desenlaces duran de horas a semanas y se solapan entre fechas vecinas (3 días de separación); el tamaño de bloque (5 fechas) es una elección de este protocolo, no se afinará con los datos.
- Una sola racha de mercado de ~2 años; el RankIC diario atenúa el efecto de régimen pero no lo elimina.
- El horizonte natural del modelo (24 h) no coincide con la duración de nuestras operaciones (salen por stop/objetivo/trailing en días).
- La fecha real de corte del preentrenamiento de los pesos públicos no está documentada; la sensibilidad ≥ 2025-03-19 y el diagnóstico pre-corte la acotan, pero no la eliminan.
- La simulación de salida usa velas de 1 h, sin slippage ni huecos.
- Un solo contraste primario; todo lo demás es exploratorio y se etiqueta así, sin corrección por comparaciones múltiples.
- El reloj de la CPU y las versiones de torch pueden producir diferencias numéricas pequeñas entre máquinas; se registran dispositivo y versiones.

## Estado

Aprobado y congelado el 2026-10-15 (decisiones del usuario: Kronos-small, corte 2024-07-01, 24 velas de horizonte con lookback 400, **cada 3 días**, **8 trayectorias**, y este protocolo con sus cuatro decisiones de diseño: RankIC diario como contraste primario, bloques de 5 fechas, mínimos de 30 fechas y 10 monedas, y diagnóstico pre-corte). Implementación: tabla y repositorio `kronos_forecasts`, reglas puras en `app/domain/rules/kronos_k1.py`, generación y unión con el desenlace en `app/experiments/kronos/k1.py`, y los scripts `scripts/kronos_k1_generar.py` y `scripts/kronos_k1_evaluar.py`. Resultados: pendientes (la generación no ha empezado).
