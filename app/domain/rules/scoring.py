"""Umbrales de puntuación fijos (sección 19 del plan, tabla "REGLAS FIJAS").

Viven en código versionado, no en `.env` (sección 10: "no conviertas reglas
críticas... en simples variables de entorno"). A diferencia de las reglas de
`risk.py` (stop, take profit, drawdown), estas dos no mueven dinero
directamente — controlan el flujo del ciclo de análisis (cuántos símbolos
pasan del pre-filtro determinista a los agentes de IA) — pero igual quedan
aquí y no dispersas en el código que las usa, por la misma razón: son
números que no deben poder cambiarse "al pasar" dentro de una función.

Estos dos valores YA estaban fijados en el plan desde el inicio (sección 19)
y no forman parte de la fórmula del pre-filtro decidida en esta sesión
(sección 6.1/17 dejaba pendiente el CÓMO se calcula el puntaje, no estos
umbrales sobre el resultado ya calculado).
"""

from __future__ import annotations

SCORE_MINIMO_PREFILTRO = 45
"""Puntaje mínimo (escala 0-100, ver `app/data/prefilter.py`) para que un
símbolo se considere candidato tras el pre-filtro determinista. Por debajo
de esto, el símbolo no llega a los agentes de IA ese ciclo. Si ningún
símbolo lo supera, el ciclo termina sin una sola llamada a la API (sección
4.2 del plan)."""

MAX_CANDIDATOS_PREFILTRO = 5
"""Máximo de candidatos que el pre-filtro entrega a la fase de agentes de
IA por ciclo (sección 19: "Máximo candidatos IA: 5"), incluso si más de 5
símbolos superan `SCORE_MINIMO_PREFILTRO` ese día."""
