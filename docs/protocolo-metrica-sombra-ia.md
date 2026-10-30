# Protocolo pre-registrado — S1: métrica a la sombra del análisis de IA (¿acierta la IA al proponer y al descartar?) — v1

- **v1 (BORRADOR)** — 2026-10-29, redactado **antes de calcular ningún desenlace** de esta pregunta. Pasa a *congelado* cuando el usuario lo apruebe; el script de evaluación calculará la huella SHA-256 de este archivo (saltos de línea normalizados), la imprimirá con el informe y se negará a producir resultados si el texto cambió respecto a la huella registrada.

Cualquier cambio posterior es un experimento NUEVO, no un ajuste de este. Es un estudio de **sombra, solo lectura**: no abre posiciones, no escribe en ninguna tabla, no modifica el score, los prompts, el pre-filtro, las compras ni ninguna regla de riesgo.

## Pregunta

Entre los candidatos que el pre-filtro entrega a la IA en un mismo ciclo, ¿los que el Consolidador **incluye en sus opciones** tienen un desenlace simulado (con nuestras reglas de salida) mejor que los que **no incluye**? ¿Y el desenlace crece con el nivel que la IA les da (omitido < NINGUNO < MAYOR_RIESGO < NORMAL)?

## Hechos de partida (medidos el 2026-10-29, solo conteos; ningún desenlace)

- Hay candidatos del pre-filtro guardados solo desde el ciclo del 2026-10-07 (antes, `prefiltro_resultados` no tenía candidatos).
- De 127 candidatos evaluados en ciclos con llamada exitosa al Consolidador, 81 (64 %) no tienen propuesta de la IA.
- Propuestas con puntaje del Consolidador: NINGUNO 29 (43,00–51,20), MAYOR_RIESGO 5 (50,04–53,10), NORMAL 1 (55,60), 7 sin tier (ciclos antiguos). El rango es estrecho (43–55,6).
- Hoy **no se puede separar** «omitido por el Consolidador» de «excluido por calidad de datos»: ambos quedan sin propuesta. Se tratan juntos como `NO_INCLUIDO` (limitación declarada).
- Las propuestas de observación (tier `OBSERVACION`) las crea el código y llevan puntaje del pre-filtro: **no son decisiones de la IA** y no cuentan como `INCLUIDO`.

## Unidad y niveles

- **Unidad:** (ciclo diario con llamada exitosa al Consolidador, símbolo candidato del pre-filtro con estado `CANDIDATO`). Ciclos sin esa llamada no entran.
- **Nivel de la IA** (excluyente):
  - `NO_INCLUIDO` (0): sin propuesta de tier distinto de `OBSERVACION` en ese ciclo y símbolo.
  - `NINGUNO` (1), `MAYOR_RIESGO` (2), `NORMAL` (3): el `tier_entrada` de la propuesta.
  - `INCLUIDO` = niveles 1 a 3. Una propuesta sin tier (ciclos antiguos) no está en el universo (no hay candidatos del pre-filtro para esos ciclos).
- El universo cambia con el tiempo (desde el 2026-10-26 los símbolos con posición abierta se excluyen del pre-filtro antes del corte de 5). El emparejamiento por ciclo lo neutraliza; se declara.

## Entrada, desenlace, censura

- **Decisión:** `started_at` del ciclo. **Entrada:** cierre de la última vela de 1 h cerrada en o antes de esa hora (misma regla que `observaciones_de_simbolo`). Solo velas posteriores a la entrada determinan el desenlace; nada futuro entra a la decisión.
- **Desenlace:** `retorno_neto` de `simular_salida` con las reglas del motor paper (stop −8 %, 50 % en +12 %, trailing 6 %, 0,1 % por lado), `SIN_FRICCION`, `MAXIMO_PRIMERO`, sin resolvedor de 1 minuto (la lectura de todos los resultados publicados).
- **Censura (primaria):** una unidad aún abierta al corte de datos se valora a mercado al cierre de la última vela de 1 h disponible (`retorno_marcado_a_mercado`), con el mismo corte para todas. La comparación es emparejada por ciclo, así que ambas partes de cada par tienen la misma antigüedad. **Sensibilidad:** solo unidades con salida resuelta.

## Contraste primario (único)

Para cada ciclo con al menos un `INCLUIDO` y al menos un `NO_INCLUIDO`: **diferencia pareada** = media del `retorno_neto` de los `INCLUIDO` − media del de los `NO_INCLUIDO`. Se promedian las diferencias de los ciclos de cada día UTC y queda una serie por día (solo días con al menos un ciclo pareado), ordenada por fecha.

**Intervalo:** `bootstrap_bloques` sobre esa serie diaria, **bloques de 3 días**, 1.000 iteraciones, semilla 20261005, percentiles 2,5 y 97,5. La elección de 3 días es de este protocolo y no se afinará: la comparación es dentro del mismo ciclo, lo que cancela el factor común de mercado y deja menos autocorrelación que un nivel.

Los ciclos con `no_invertir` (todos `NO_INCLUIDO`) no producen par; se analizan en el secundario 1.

## Contrastes secundarios (exploratorios; no deciden)

1. **Ciclos `no_invertir` vs `invertir`:** diferencia entre el retorno medio de todos los candidatos de ciclos `no_invertir` y el de todos los candidatos de ciclos `invertir`, serie diaria, mismo bootstrap.
2. **Escala ordenada:** RankIC (Spearman) entre el nivel 0–3 y el `retorno_neto`, por ciclo con al menos 2 niveles distintos y 3 unidades; se promedia por día y se aplica el mismo bootstrap.
3. **Puntaje dentro de lo incluido:** RankIC entre `puntaje_ponderado` y `retorno_neto` por ciclo con ≥ 3 propuestas con puntaje; misma agregación.
4. **Candidatos vs resto de la lista autorizada:** diferencia pareada por ciclo entre el retorno medio de los candidatos y el de los demás símbolos con datos (`DESCARTADO_*`), mismo bootstrap. Es una validación hacia adelante del pre-filtro, no de la IA.
5. **Tasa de acierto descriptiva:** proporción con `retorno_neto > 0` en `INCLUIDO` y en `NO_INCLUIDO`, sin inferencia.
6. **Solo resueltas** (sensibilidad de censura) del contraste primario.
7. **Confusión con el pre-filtro:** diferencia pareada del `score_final` del pre-filtro entre `INCLUIDO` y `NO_INCLUIDO`. Si es grande, el contraste primario puede reflejar el pre-filtro y no la IA; se reporta, sin regla automática.
8. **Composición:** conteos por nivel, símbolo, día, `prompt_version` del Consolidador. No se estratifica por versión; se reporta.

## Regla de decisión (fija)

- **NO_CONCLUYENTE_MUESTRA_INSUFICIENTE:** menos de 30 días con ciclo pareado, o menos de 30 ciclos pareados, o menos de 30 unidades `INCLUIDO` resueltas por valoración a mercado.
- **NO_DISCRIMINA:** el límite inferior del IC95 de la media de la serie diaria es ≤ 0.
- **DISCRIMINA:** el límite inferior es > 0.

**Alcance.** Incluso `DISCRIMINA` no cambia ninguna regla: justifica seguir acumulando muestra y, en su caso, proponer una enmienda con datos no vistos. `NO_DISCRIMINA` con muestra pequeña **no** prueba que la IA no sirva: el estudio solo detecta diferencias grandes (la desviación típica del retorno por operación, ~8 pp, es una suposición no medida aquí).

## Cuándo se evalúa

Mientras no se cumplan los mínimos, el script solo imprime **conteos** (modo conteo: días, ciclos pareados, unidades por nivel) y **no calcula ningún desenlace**, para no espiar resultados. La evaluación primaria se corre **una sola vez**, el primer día en que se cumplan los tres mínimos; las corridas posteriores se reportan como actualizaciones, sin sustituir a la primaria.

## Prohibido tras ver resultados

Cambiar la definición de nivel, el criterio `INCLUIDO`, el universo, la regla de entrada, el desenlace, la valoración de abiertas, el tamaño de bloque, las iteraciones, la semilla, los mínimos, el contraste primario o la regla de decisión; excluir días o símbolos; probar variantes hasta que una «salga». Cualquier variante es un experimento nuevo y se declara como tal.

## Operación (sin efecto estadístico)

- Script de solo lectura `scripts/estudio_sombra_ia.py` (a escribir tras la aprobación): lee `ciclos_analisis`, `prefiltro_resultados`, `propuestas`, `llamadas_ia` y velas; reutiliza `simular_salida`, `retorno_marcado_a_mercado` y `bootstrap_bloques`. Sin tablas nuevas, sin escrituras, sin red ni IA.
- Cualquier cambio de lo que se guarda (p. ej. una fila por candidato con propuesto/omitido/excluido) es una decisión aparte y no forma parte de este protocolo.

## Limitaciones declaradas de antemano

- Muestra pequeña y de pocas semanas; los mismos símbolos se repiten día tras día; desenlaces solapados.
- `NO_INCLUIDO` mezcla omitidos y excluidos por calidad de datos; el Consolidador no puntúa a los que omite, así que no hay puntaje que comparar en ese grupo.
- Selección: el nivel de la IA solo existe para lo que la IA decidió proponer.
- Los prompts han cambiado entre ciclos; el grupo `INCLUIDO` no es homogéneo.
- Valorar a mercado las abiertas mezcla resultados realizados y no realizados.
- Sin slippage ni huecos (`SIN_FRICCION`); velas de 1 h.
- Un solo régimen de mercado; sin corrección por comparaciones múltiples en los secundarios.

## Estado

Borrador pendiente de aprobación. Ningún desenlace calculado. Resultados: pendientes.
