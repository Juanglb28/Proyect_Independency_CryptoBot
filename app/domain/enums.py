"""Enums de dominio (sección 7 del plan: `app/domain/enums.py`).

Sin dependencias de Streamlit, Binance, Anthropic ni SQLAlchemy — solo
`enum` de la librería estándar, igual que el resto de `app/domain`.
"""

from __future__ import annotations

from enum import Enum


class EstadoCiclo(str, Enum):
    """Etapas de `ciclos_analisis.status` (sección 15.6 del plan, "Contrato
    del ciclo diario").

    Paso 7 (`app/orchestration/daily_cycle.py`) implementa el tramo de
    ANÁLISIS del ciclo: desde la ingesta hasta congelar el
    `decision_snapshot` de cada propuesta. Solo existen aquí las 8 etapas
    que ese código realmente ejecuta y transiciona — decisión del usuario
    (2026-09-30): el resto de la máquina de estados de la sección 15.6
    (RISK_VALIDATION, PAPER_ORDER, POSITION, DETERMINISTIC_MONITORING,
    TRADE_RESULT, PERFORMANCE_RECALCULATION) pertenece al motor de
    ejecución (Paso 8) y a resultados/performance (Paso 9): agregar esos
    valores ahora, sin ningún código que los use, sería precisamente la
    "implementación arbitraria" que la sección 29 del master prompt pide
    evitar. Se amplía este enum cuando esos pasos se implementen.

    `ciclos_analisis.status` guarda la ÚLTIMA etapa alcanzada, no un
    resultado — la sección 15.6 exige que "un fallo en una etapa no debe
    permitir saltar silenciosamente a una etapa posterior": el resultado
    real de un ciclo se deriva de tres cosas, nunca de un valor adicional
    inventado para "resumir" el estado:

    - `error_message` no nulo -> el ciclo falló exactamente en `status`.
    - `error_message` nulo y `status == CONSOLIDATE` con >=1 fila en
      `propuestas` para ese `ciclo_id` -> terminó con propuestas.
    - `error_message` nulo pero sin propuestas -> terminó sin candidatos
      (pre-filtro vacío, Auditor bloqueó el ciclo, o el Consolidador
      recomendó `no_invertir`) — todas variantes normales, no un error.
    """

    INGEST = "INGEST"
    VALIDATE_DATA = "VALIDATE_DATA"
    CALCULATE_INDICATORS = "CALCULATE_INDICATORS"
    PREFILTER = "PREFILTER"
    EVENT_CORRELATION = "EVENT_CORRELATION"
    AI_ANALYSIS = "AI_ANALYSIS"
    CONSOLIDATE = "CONSOLIDATE"
    FREEZE_DECISION_SNAPSHOT = "FREEZE_DECISION_SNAPSHOT"
