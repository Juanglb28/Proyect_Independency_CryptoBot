# Protocolo pre-registrado — D1: distancia del precio a la media de 200 periodos (4 h) en BTCUSDT — v1

- **v1** — 2026-11-17, redactado **antes de calcular ninguna distancia ni ningún retorno de esta pregunta**. **Aprobado por el usuario el 2026-11-17 y congelado.** La huella SHA-256 de este archivo (saltos de línea normalizados) está registrada en `data/distancia_ma200/protocolo_sha256.txt`; `scripts/estudio_distancia_ma200.py` la imprime con cada informe y se niega a trabajar si el texto cambió. Cualquier cambio posterior es un experimento NUEVO, no un ajuste de este.

Es un estudio **exploratorio, de solo lectura**: no abre posiciones, no escribe en ninguna tabla, no llama a la IA, no añade dependencias y no modifica el score, los prompts, el pre-filtro, las compras ni ninguna regla de riesgo. No es una recomendación de inversión.

## Origen y decisiones del usuario (2026-11-17)

Observación del usuario en TradingView (BTC/USDT, 4 h): con la media de 200 periodos, el precio oscila hasta ~8 %–19 % por encima y hasta ~25 % por debajo, y «en lo revisado de este año» no rompió esos rangos. Decisiones aprobadas: **(1)** la media primaria es la **SMA**, con la **EMA** como comprobación; **(2)** el alcance es **solo BTCUSDT**.

## Preguntas

- **A (descriptiva).** ¿Cómo se distribuye la distancia del precio a la media en toda la historia disponible? ¿Cuántas veces y cuándo se superaron +19 % y −25 %? ¿Es normal que una ventana de un año no contenga ninguna ruptura?
- **B (predictiva).** ¿Estar en una cola extrema de esa distribución (medida solo con datos pasados) anticipa un retorno posterior distinto del de las demás velas?

## Datos

- **Serie:** velas de **1 h cerradas** de BTCUSDT agregadas a **4 h** en bloques UTC 00-04-08-12-16-20. Una vela de 4 h solo es válida si existen sus **cuatro** velas de 1 h cerradas; si falta alguna, la de 4 h no existe (no se rellena) y se cuenta.
- **Ventana:** toda la historia de 1 h de BTCUSDT que haya en la base al correr. La cobertura real se mide primero en **modo conteo** (sin calcular distancias ni retornos) y se informa antes de cualquier resultado.
- **Calentada:** la SMA exige 200 cierres de 4 h **consecutivos** (sin vela faltante dentro de la ventana; si falta, esa observación no existe y se cuenta). Para que la siembra de la EMA no pese, **SMA y EMA se evalúan sobre la misma muestra, desde la vela 600 (3 × 200) en adelante**.
- **Cierre de datos:** solo velas cerradas; la última vela cerrada disponible fija el corte, igual para todo.

## Definiciones

- **Distancia:** `dist_t = cierre_t / MA_t − 1`, con la media calculada con los 200 cierres de 4 h que terminan en `t` (incluye la vela `t`, ya cerrada). La decisión se toma al cierre de `t`: nada posterior entra.
- **SMA:** media simple de 200 cierres. **EMA:** α = 2/201, sembrada con la SMA de los primeros 200 cierres. Aritmética con `Decimal` (la escala de TradingView puede diferir en decimales lejanos; no afecta a lecturas en puntos porcentuales).
- **Retorno posterior:** `r_h(t) = cierre_{t+h} / cierre_t − 1` con `h` ∈ {6, 18, 42} velas de 4 h (1, 3 y 7 días). Entrada = cierre de `t`, **sin comisiones ni deslizamiento** (se menciona 0,1 % por lado solo como referencia para interpretar). Sin retorno si faltan velas hasta `t+h`; esas observaciones se excluyen y se cuentan.
- **Percentil móvil:** `pct_t` = posición de `dist_t` entre las `dist` de **todas las velas anteriores** de la muestra (ventana creciente; nunca se usa una distribución que incluya el futuro). Las primeras 1.080 velas elegibles (180 días) no se clasifican (calentada del percentil).
- **Grupos:** `COLA_BAJA` si `pct ≤ 5`, `COLA_ALTA` si `pct ≥ 95`, `CENTRO` en el resto.
- **Episodio:** racha de velas consecutivas en una misma cola; un episodio nuevo empieza tras al menos 6 velas (1 día) fuera de esa cola. Es la medida honesta del tamaño de muestra de las colas (las velas de una racha no son independientes).

## Parte A — descripción (sin hipótesis)

- **A1.** Distribución de `dist` (mínimo, P1, P5, P25, P50, P75, P95, P99, máximo) en toda la muestra y por año calendario UTC, incluido el año en curso hasta el corte.
- **A2.** Frente a lo observado por el usuario: % de velas con `dist > +19 %` y con `dist < −25 %`, y lista de episodios (inicio, fin, exceso máximo) de cada una.
- **A3.** «¿No rompió en un año?»: para cada ventana móvil de 365 días (paso de 1 día), si contiene alguna vela fuera de [−25 %, +19 %]; se informa la fracción de ventanas **sin** ruptura y las fechas de las rupturas.

## Parte B — predicción (hipótesis fijadas de antemano)

- **Hipótesis (dirección esperada, de reversión a la media):** tras `COLA_BAJA` el retorno posterior es **mayor** que tras `CENTRO`; tras `COLA_ALTA` es **menor**.
- **Contrastes primarios (2):** diferencia de medias de `r_18` entre `COLA_BAJA` y `CENTRO`, y entre `COLA_ALTA` y `CENTRO`, con la **SMA**, en toda la muestra. El horizonte primario es 18 velas (3 días) y se fija aquí; no se elige después.
- **Intervalo:** remuestreo por **bloques de 14 días consecutivos** (cubre dos veces el horizonte más largo), 1.000 iteraciones, semilla 20261005, percentiles 2,5 y 97,5; la estadística se recalcula con las velas de los días remuestreados. El bloque de 14 días es de este protocolo y no se afinará.
- **Veredicto por cola (solo el contraste primario decide):**
  - `NO_CONCLUYENTE_MUESTRA_INSUFICIENTE`: menos de **10 episodios** en esa cola.
  - `SIN_EVIDENCIA`: el intervalo contiene 0.
  - `EN_LA_DIRECCION_ESPERADA`: el intervalo excluye 0 en la dirección de la hipótesis.
  - `EN_CONTRA`: el intervalo excluye 0 en la dirección opuesta.
- **Secundarios (exploratorios; no deciden):** `h` = 6 y 42; la **EMA**; primera mitad frente a segunda mitad de la muestra (corte por fecha mediana); y la versión con **umbrales fijos del usuario** (`dist > +19 %` / `< −25 %`). Esta última queda **contaminada por haberse visto en el gráfico** y se rotula como solo descriptiva.
- Siempre se imprimen n de velas, n de días y **n de episodios** de cada grupo, para ver el tamaño real de la muestra.

## Controles de validez (condición para leer cualquier resultado)

1. **Agregación:** donde existan velas de 4 h guardadas en la base, la vela de 4 h agregada desde 1 h debe coincidir **exactamente** en apertura, máximo, mínimo y cierre. Si no coincide, el estudio no es válido y se corrige antes de leer nada.
2. **Media:** si la tabla `indicadores` trae una media de 200 de 4 h para BTCUSDT, debe coincidir con la calculada aquí en el tramo común; si no la trae, se declara que no hay control cruzado.
3. **Sin información futura:** pruebas unitarias demuestran que `dist_t`, `pct_t` y el grupo de `t` no cambian si se alteran o eliminan las velas posteriores a `t`.

## Cautelas declaradas

Una sola serie y pocos episodios de cola (los extremos son raros y agrupados): el intervalo es optimista aun con bloques. Las 2 pruebas primarias (más ~10 secundarias) no llevan corrección por comparaciones múltiples; un resultado aislado no se tratará como señal. Los retornos son brutos, sin costos. Un régimen de mercado largo puede dominar la muestra. La reversión a la media de una media móvil es en parte mecánica (la media sigue al precio). Nada de esto autoriza capital real ni cambia la plataforma: **incorporar esta distancia como indicador, score o filtro exigiría una enmienda nueva con su propio protocolo**, y el estudio de ningún modo reajusta la fórmula vigente.

## Implementación prevista (después de aprobar y congelar)

- `app/domain/rules/distancia_ma.py`: funciones puras (agregación 1 h → 4 h, SMA, EMA, distancia, percentil móvil, episodios, retornos posteriores, bootstrap por bloques de días).
- `scripts/estudio_distancia_ma200.py`: solo lectura (transacción `READ ONLY`), con **modo conteo** (`--solo-contar`: cobertura y control 1, sin distancias ni retornos) y huella del protocolo en cada informe.
- Pruebas en `tests/unit/`, incluida la de ausencia de información futura.
- Sin dependencias nuevas; costo cero; sin IA.

## Aclaraciones de implementación (fijadas el 2026-11-17, antes de escribir código y de mirar datos; no cambian ninguna regla)

1. **Día de una observación:** la fecha UTC de la **apertura** de su vela de 4 h. Los bloques del bootstrap son de 14 **días de calendario** consecutivos (los días sin observaciones del grupo cuentan como vacíos).
2. **Episodios:** se cuentan sobre todas las velas **clasificadas** de la cola (a partir de la 1.081.ª elegible), con independencia de que tengan retorno posterior. Un episodio nuevo empieza si entre dos velas de la cola median 6 o más velas fuera de ella.
3. **`CENTRO`** es solo `5 < pct < 95`; la cola opuesta no entra en ninguna de las dos partes de un contraste.
4. **Percentil:** `pct_t = 100 × (nº de distancias anteriores estrictamente menores que dist_t) / (nº de distancias anteriores)`, con las distancias de todas las velas elegibles anteriores. SMA y EMA tienen cada una su propio historial.
5. **Posición 600:** se cuenta desde el inicio de cada tramo continuo de velas de 4 h (un hueco reinicia el tramo; SMA y EMA se vuelven a sembrar). La distancia existe desde la vela 600 del tramo.
6. **Primera mitad / segunda mitad:** corte en la fecha de la vela clasificada que ocupa la posición mediana; la primera mitad es estrictamente anterior a esa fecha.
7. **Umbrales fijos (secundario descriptivo):** `dist_SMA > +19 %` frente a `CENTRO`, y `dist_SMA < −25 %` frente a `CENTRO`, sobre la misma muestra clasificada.
8. **Informe:** la primera evaluación se guarda como `evaluacion-primaria-*.json`; las siguientes, como `actualizacion-*.json`, y no sustituyen a la primaria (la base crece cada día; así no se elige después cuál mirar).

## Estado

2026-11-17: **APROBADO y CONGELADO.** No se ha mirado ningún dato de esta pregunta. Primer paso: el modo conteo (cobertura y control de agregación, sin distancias ni retornos).
