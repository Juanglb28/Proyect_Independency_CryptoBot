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
    ANÁLISIS del ciclo, leyendo velas/indicadores YA calculados en la base
    de datos — nunca los recalcula (ver el docstring del propio
    orquestador). Por eso este enum NO incluye `INGEST`/`VALIDATE_DATA`/
    `CALCULATE_INDICATORS` de la sección 15.6: la ingesta real
    (`scripts/backfill_candles.py`) y el cálculo de indicadores
    (`scripts/calculate_indicators.py`) siguen siendo procesos separados y
    manuales (el README ya documenta esto como pendiente: "actualización
    incremental programada... APScheduler queda para cuando exista el
    orquestador" — ese orquestador de ingesta automática todavía no
    existe). Agregar esas 3 etapas aquí, sin ningún código que las
    transite, sería precisamente la "implementación arbitraria" que la
    sección 29 del master prompt pide evitar.

    Solo existen aquí las 5 etapas que este orquestador realmente ejecuta
    y transiciona. El resto de la máquina de estados de la sección 15.6
    (RISK_VALIDATION, PAPER_ORDER, POSITION, DETERMINISTIC_MONITORING,
    TRADE_RESULT, PERFORMANCE_RECALCULATION) pertenece al motor de
    ejecución (Paso 8) y a resultados/performance (Paso 9) — se amplía
    este enum cuando esos pasos se implementen.

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

    PREFILTER = "PREFILTER"
    EVENT_CORRELATION = "EVENT_CORRELATION"
    AI_ANALYSIS = "AI_ANALYSIS"
    CONSOLIDATE = "CONSOLIDATE"
    FREEZE_DECISION_SNAPSHOT = "FREEZE_DECISION_SNAPSHOT"


class TierEntrada(str, Enum):
    """Clasificación de una `propuesta` según cómo (o si) calificaría para
    una entrada — decisión del usuario (2026-10-04), ver
    `app/domain/rules/entry_tier.py`.

    - `NORMAL`: `puntaje_ponderado >= SCORE_MINIMO_ENTRADA` (55). Es la
      regla original del plan, sin cambios.
    - `MAYOR_RIESGO`: no superó 55, pero cayó en la zona cercana (>= 50) y
      pasó TODAS las verificaciones de la segunda ronda. Es un tier
      separado y etiquetado a propósito: la v1 existe para medir si el
      score tiene poder predictivo (sección 1 del master prompt), y mezclar
      estas entradas con las normales en una misma muestra impediría saber
      si el criterio relajado rinde mejor o peor.
    - `NINGUNO`: no califica por ninguna de las dos vías.

    Se guarda como VARCHAR validado en Python (`native_enum=False`), igual
    que `EstadoCiclo`, para no necesitar un `ALTER TYPE` si el criterio
    evoluciona."""

    NORMAL = "NORMAL"
    MAYOR_RIESGO = "MAYOR_RIESGO"
    NINGUNO = "NINGUNO"


class LadoOrden(str, Enum):
    """Lado de una orden. En v1 (spot, sin apalancamiento ni cortos — sección
    3 del plan) `BUY` solo abre/aumenta una posición LONG y `SELL` solo la
    reduce o cierra."""

    BUY = "BUY"
    SELL = "SELL"


class IntencionOrden(str, Enum):
    """Para qué existe una orden (`ordenes.intencion`). Los valores de salida
    son EXACTAMENTE los valores controlados de `exit_reason` de la sección
    8.4 del plan (así `trade_results.exit_reason` sale directo de la orden de
    salida, sin traducir); `ENTRY` es la única que abre una posición.

    Decisión del usuario (2026-10-04): este diseño de `ordenes` queda
    aprobado antes de escribir la migración — ver README, "Decisiones del
    Paso 8". Se guarda como VARCHAR validado (`native_enum=False`), igual
    que `EstadoCiclo`."""

    ENTRY = "ENTRY"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT_PARTIAL = "TAKE_PROFIT_PARTIAL"
    TRAILING_STOP = "TRAILING_STOP"
    MANUAL_APPROVAL_EXIT = "MANUAL_APPROVAL_EXIT"
    SYSTEM_DRAWDOWN = "SYSTEM_DRAWDOWN"
    RECONCILIATION_EXIT = "RECONCILIATION_EXIT"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    OTHER = "OTHER"


class EstadoOrden(str, Enum):
    """Estado de `ordenes.estado` (decisión del usuario, 2026-10-04).

    - `PENDING_APPROVAL`: entrada que exige aprobación manual (mayor riesgo,
      o sobre 200 USD) y todavía no se resolvió. Vive aquí y no en
      `propuestas` para que la propuesta y su snapshot sigan siendo historia
      congelada (sección 14 del plan).
    - `FILLED`: ejecutada en paper. Una orden paper se ejecuta en la misma
      transacción que actualiza la posición — por eso no hay un estado
      intermedio "enviada/sin llenar" todavía (P5): si en el futuro existe un
      exchange real con latencia, se agrega con su propio caso de prueba.
    - `REJECTED`: el usuario la rechazó, o la validación de riesgo la bloqueó.
    - `EXPIRED`: no se aprobó antes del fin del día UTC del ciclo (00:00 UTC del
      día siguiente)."""

    PENDING_APPROVAL = "PENDING_APPROVAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class EstadoPosicion(str, Enum):
    """`posiciones.status`: `OPEN` mientras quede cantidad, `CLOSED` cuando
    la cantidad restante llega a 0 (un cierre parcial sigue siendo `OPEN`)."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


class TipoEventoSistema(str, Enum):
    """Tipos de `eventos_sistema.event_type` (decisión del usuario,
    2026-10-04: la pausa por drawdown se guarda como un registro de eventos
    de solo-agregar, no como un campo que se sobrescribe).

    - `PAUSA_DRAWDOWN`: el equity cayó a -15% o menos desde su pico y el
      sistema dejó de abrir entradas nuevas.
    - `REANUDACION`: el usuario reanudó a mano. Fija un pico de equity NUEVO
      igual al equity de ese momento (si no, la regla `equity <= pico × 0,85`
      volvería a pausar al instante) y conserva el pico anterior como
      registro.

    El estado actual es el tipo del ÚLTIMO evento (ver
    `app.domain.rules.risk.sistema_pausado`). Se guarda como VARCHAR validado
    (`native_enum=False`), igual que el resto de enums."""

    PAUSA_DRAWDOWN = "PAUSA_DRAWDOWN"
    REANUDACION = "REANUDACION"
