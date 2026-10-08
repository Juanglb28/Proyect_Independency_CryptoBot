# Protocolo pre-registrado — Hipótesis H1 (soporte + RSI bajo → recuperación) — v2

- **v1** — 2026-10-08, parámetros aprobados por el usuario (métrica, cercanía 2 %, RSI 4h < 35). Entregada en el patch 0006.
- **v2** — 2026-10-08, correcciones tras la revisión del usuario, **registradas antes de calcular ningún resultado de H1** (se incorporan al repositorio junto con el código para que las reglas fijadas antes de correr sean verificables mediante el historial de Git y la huella SHA-256 que imprime el script).

Cualquier cambio posterior a este texto es una hipótesis NUEVA, no un ajuste de esta. Es un estudio offline y **exploratorio**: no modifica el score, el prompt ni ninguna regla viva.

## Cambios de la v2 respecto a la v1

1. **Línea base ajustada.** Además de la línea base bruta (todos los días elegibles), se compara contra una línea base ajustada por moneda y periodo de mercado (ver "Línea base ajustada"). Para pasar hay que superar **ambas**.
2. **Purga de desenlaces en el corte 70/30.** En exploración se excluyen las observaciones cuyo desenlace cae en o después del corte (o no se resolvió). Ver "Partición".
3. **Retorno neto de la salida real (secundario).** Simulación con cierre parcial del 50 % en +12 %, trailing del 6 % sobre el resto y comisión 0,1 % por lado. Descriptivo; **no decide el veredicto**.
4. **Veredicto por definición.** Cada definición es PASA / NO_PASA / SIN_EVALUAR_MUESTRA_INSUFICIENTE; una definición sin muestra suficiente no cuenta como "no respaldada".
5. **Carácter exploratorio explícito** y el protocolo vive en el repositorio (`docs/protocolo-h1-soporte.md`).

## Hipótesis

H1: entrar cuando el precio está cerca de un soporte relevante y el RSI de 4h está en sobreventa produce, con las barreras vigentes (stop −8 % / objetivo +12 %), una tasa de "objetivo primero" mayor que entrar sin condición.

## Dos definiciones de soporte (se comparan; ambas fijadas ahora)

- **S1 — mínimo de oscilación (fractal diario).** Una vela diaria es mínimo de oscilación si su mínimo (low) es ESTRICTAMENTE menor que el de cada una de las 5 velas anteriores y de las 5 siguientes (N = 5). Solo se conoce cuando cerró la 5.ª vela posterior. Soporte vigente en el día de decisión = entre los mínimos de oscilación ya confirmados dentro de las últimas 180 velas diarias, el de mayor nivel que esté por debajo o igual al último cierre diario. Si no hay, no hay señal.
- **S2 — mínimo de 90 días.** Mínimo de los low de las últimas 90 velas diarias cerradas (espejo del "máximo de 90 días" que ya existe).

## Condición de señal

Cerca del soporte: `nivel ≤ cierre diario ≤ nivel × 1,02` (tolerancia de 2 %). **Y** RSI-14 (Wilder) de la última vela de 4h cerrada **< 35**.

## Controles y línea base

- Control A: solo "cerca del soporte" (sin RSI), para S1 y para S2.
- Control B: solo "RSI 4h < 35" (sin soporte).
- **Línea base bruta:** todos los días elegibles de las mismas monedas, sin condición.
- **Línea base ajustada:** ver abajo.

## Mecánica de cada observación (sin look-ahead)

- Una decisión por moneda y día a las 00:00 UTC del día D. Solo velas CERRADAS: diarias con apertura < D, de 4h ya cerradas a las 00:00 de D, y entrada = cierre de la vela de 1h que cierra a las 00:00 de D.
- Desenlace: se recorren las velas de 1h posteriores hasta el primer toque de stop −8 % (`STOP_LOSS_PCT`) u objetivo +12 % (`TAKE_PROFIT_PCT`), con la misma convención de igualdad que `app/domain/rules/risk.py`. Sin horizonte máximo. Velas que tocan ambos niveles = AMBIGUA; sin tocar ninguno = SIN_RESOLVER; ambas se reportan y NO entran en la tasa.
- Día elegible: requiere ≥ 190 velas diarias previas y ≥ 200 velas de 4h previas (calentamiento de los indicadores). Mismas condiciones de elegibilidad para S1, S2, controles y línea base.
- Universo: los 20 símbolos autorizados; ~2 años de velas cerradas guardadas. **Requisito:** historial reparado (el script se niega a correr si hay velas vencidas sin cerrar).

## Muestra de análisis

- **Primaria: episodios.** Un episodio = primer día de una racha de días consecutivos con la condición verdadera en la misma moneda (reduce solapamiento). Secundaria (informativa): todas las señales.
- **Partición temporal:** exploración = primer 70 % del rango de fechas elegibles (70/30); confirmación = último 30 %. Una observación pertenece a la partición de su fecha de decisión.
- **Purga (v2).** En exploración se excluyen las observaciones (de la condicional y de la línea base) cuyo desenlace —instante en que se tocó una barrera— es igual o posterior a la medianoche UTC de la fecha de corte, o no se resolvió con los datos disponibles. Así elegir entre S1 y S2 no usa información del tramo reservado para confirmar. La confirmación NO se purga (su desenlace puede resolverse hasta el final de los datos; las no resueltas se reportan como SIN_RESOLVER). Para la métrica secundaria la purga usa el cierre total de la salida simulada. El informe imprime cuántos episodios se purgaron.

## Estadística

- Métrica principal: `tasa_objetivo = objetivo_primero / (objetivo_primero + stop_primero)`.
- Diferencia = tasa(condicional, episodios) − tasa(línea base), calculada contra la línea base bruta y contra la ajustada.
- **Línea base ajustada.** Estrato = (moneda, mes calendario) de la fecha de decisión. Para cada estrato donde la condicional tiene episodios resueltos Y la línea base también tiene observaciones resueltas, se toma la tasa de la línea base DE ESE estrato; la línea base ajustada es el promedio de esas tasas ponderado por el número de episodios resueltos de la condicional en cada estrato. Los estratos sin datos en alguno de los dos lados se descartan (se imprime cuántos estratos se usaron).
- Intervalo: bootstrap **por clúster de fecha** (se remuestrean días calendario con reemplazo, porque las monedas se mueven en bloque), 2000 iteraciones, semilla fija 20261008, mismas multiplicidades para todos los grupos, **intervalo percentil de 97,5 %** (corrección de Bonferroni por comparar dos definiciones: percentiles 1,25 y 98,75). Implementación en Python puro; sin dependencias nuevas.
- Punto de equilibrio: `break_even_target_rate` de `app/data/barrier_study.py` (comisión 0,1 % por lado).

## Métrica secundaria (no decide): retorno neto de la salida real

Para cada observación se simula la salida del motor paper sobre velas de 1h con las mismas constantes de `app/domain/rules/risk.py`: cierre del 50 % al tocar +12 %, trailing del 6 % sobre el resto, stop −8 % antes del parcial, comisión 0,1 % por lado, sin slippage. Convenciones pesimistas sobre OHLC: si una vela toca el stop y el objetivo, cuenta como stop; tras el parcial el resto de esa vela no se evalúa y el máximo del trailing arranca en el nivel del objetivo; luego cada vela actualiza el máximo con su `high` y después compara su `low` con el trailing. Las posiciones que no se cierran del todo con los datos disponibles quedan censuradas y no se cuentan. Se reporta el retorno medio frente a la línea base bruta y ajustada con su intervalo, solo como contexto: una tasa de objetivo favorable que no se traduzca en retorno neto es una señal de cautela, pero no cambia el veredicto.

## Regla de decisión (fija)

Cada definición se juzga POR SEPARADO en la partición de **confirmación**:

- **SIN_EVALUAR_MUESTRA_INSUFICIENTE:** menos de 30 episodios resueltos (objetivo + stop).
- **PASA:** con ≥ 30 episodios resueltos, (a) el límite inferior del intervalo de la diferencia es > 0 frente a la línea base bruta **y** frente a la ajustada, y (b) la tasa condicional supera el punto de equilibrio.
- **NO_PASA:** con ≥ 30 episodios resueltos, no cumple (a) y (b).

Veredicto:

- Si al menos una definición PASA: **H1_RESPALDADA** (con la definición ganadora; si pasan ambas, gana la de mayor valor de min(diferencia bruta, diferencia ajustada) en exploración). Una definición sin evaluar no impide el veredicto.
- Si ninguna PASA y ambas fueron evaluadas: **H1_NO_RESPALDADA**.
- Si ninguna PASA, una fue evaluada y la otra quedó SIN_EVALUAR_MUESTRA_INSUFICIENTE: **H1_NO_RESPALDADA_EN_LAS_EVALUADAS_OTRAS_SIN_EVALUAR** (la definición sin evaluar no se declara refutada).
- Si ninguna alcanzó los 30 episodios: **NO_CONCLUYENTE_MUESTRA_INSUFICIENTE**.

**Alcance del veredicto.** El estudio es exploratorio (una sola racha de mercado de ~2 años, varias comparaciones). Incluso un veredicto H1_RESPALDADA solo justifica **continuar la prueba en paper con datos nuevos**; NO incorpora la señal al score. Incorporarla requeriría proponer un factor nuevo (versión nueva de score/prompt), aprobación explícita y validación hacia delante con muestra nueva.

## Prohibido tras ver resultados

Cambiar N, el 2 %, el 35, las ventanas de 180 y 90 días, las barreras, el corte 70/30, la hora de decisión, la definición de estrato, el mínimo de 30 episodios o la regla de decisión. Cualquier variante es una hipótesis nueva con su propia corrección.

## Limitaciones declaradas de antemano

Monedas correlacionadas (el bootstrap por fecha lo mitiga, no lo elimina); una sola racha de mercado de ~2 años (el régimen puede dominar; el ajuste por mes lo reduce, no lo elimina); mes calendario como "periodo de mercado" es una aproximación gruesa; el estudio mide barreras con velas de 1h, no el comportamiento exacto del monitor de 60 s; la simulación de salida no modela slippage ni huecos de precio; no incluye el score ni la IA; con pocos episodios por estrato el ajuste es ruidoso.

## Estado

Parámetros aprobados por el usuario (métrica, 2 %, RSI 4h < 35, mínimo de 30 episodios). Implementación: `app/domain/rules/support.py`, `app/data/support_study.py`, `app/data/exit_simulation.py`, `scripts/study_support_h1.py`. Resultados: pendientes (se corre en la máquina del usuario con el historial reparado).
