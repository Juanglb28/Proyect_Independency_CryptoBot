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
    - `OBSERVACION` (decisión del usuario, 2026-10-12): NO es una propuesta
      de la estrategia. Es una operación paper abierta por regla sobre un
      candidato del pre-filtro (puntaje >= 45) que no abrió una entrada de la
      estrategia, para ir reuniendo operaciones con una variedad de puntajes
      mientras la estrategia casi no califica. No pasa por IA, tiene cupos
      propios, no cuenta en el equity ni en la pausa por drawdown y se mide
      como SERIE APARTE (ver `app/domain/rules/observation.py`).

    Se guarda como VARCHAR validado en Python (`native_enum=False`), igual
    que `EstadoCiclo`, para no necesitar un `ALTER TYPE` si el criterio
    evoluciona."""

    NORMAL = "NORMAL"
    MAYOR_RIESGO = "MAYOR_RIESGO"
    NINGUNO = "NINGUNO"
    OBSERVACION = "OBSERVACION"


class EstadoPrefiltro(str, Enum):
    """Resultado del pre-filtro de UN símbolo en UN ciclo (tabla
    `prefiltro_resultados`). Se deriva de las reglas ya fijadas
    (`SCORE_MINIMO_PREFILTRO` y `MAX_CANDIDATOS_PREFILTRO`); no es una regla
    nueva."""

    CANDIDATO = "CANDIDATO"
    """Superó el puntaje mínimo y quedó entre los mejores del tope."""
    DESCARTADO_POR_PUNTAJE = "DESCARTADO_POR_PUNTAJE"
    """Tuvo puntaje, pero por debajo del mínimo del pre-filtro."""
    DESCARTADO_POR_TOPE = "DESCARTADO_POR_TOPE"
    """Superó el mínimo, pero había más símbolos mejores que el tope de candidatos."""
    DESCARTADO_POR_POSICION_ABIERTA = "DESCARTADO_POR_POSICION_ABIERTA"
    """Superó el mínimo, pero el símbolo ya tiene una posición abierta o una entrada
    pendiente (de cualquier serie): no se vuelve a analizar con IA, porque el sistema
    no aumenta posiciones (decisión del 2026-10-26). No consume cupo de candidatos."""
    SIN_DATOS = "SIN_DATOS"
    """Faltaba algún dato (velas, SMA, volumen relativo o distancia a 90d): no
    recibió puntaje (P3: nada de valores de relleno)."""


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


class SerieOperacion(str, Enum):
    """A qué serie pertenece una operación (decisión del usuario, 2026-10-12).

    - `ESTRATEGIA`: las entradas de la estrategia real (tier `NORMAL` o
      `MAYOR_RIESGO`, y las propuestas anteriores al tier, que son NULL).
    - `OBSERVACION`: las operaciones de observación (tier `OBSERVACION`).

    No es una columna: se DERIVA del tier de la propuesta de la que sale la
    operación, para que haya una sola fuente de verdad (ver
    `app/repositories/series.py`). Las dos series se miden por separado."""

    ESTRATEGIA = "ESTRATEGIA"
    OBSERVACION = "OBSERVACION"


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


class TipoAlerta(str, Enum):
    """Tipos de `alertas.tipo` (Paso 11a): avisos que el sistema le manda al
    usuario por Telegram. Son SOLO avisos: ninguno cambia una orden, una
    posición ni una regla de riesgo.

    Los cinco escalamientos de la sección 9 del plan y del master prompt:
    - `CAIDA_1H`: una posición abierta cayó más de 5 % en la última hora.
    - `VOLUMEN_ATIPICO`: el volumen de la última vela 1h cerrada de una
      posición abierta superó 3 desviaciones estándar (decisión D3).
    - `ORDEN_PENDIENTE_APROBACION`: hay una entrada esperando la aprobación
      manual del usuario (las de más de 200 USD y las de mayor riesgo).
    - `BINANCE_CAIDO` / `BINANCE_RESTABLECIDO`: Binance no respondió durante
      más de 5 minutos / volvió a responder (decisión D5; el estado es el tipo
      de la última alerta de este par).
    - `DISCREPANCIA_RECONCILIACION`: el estado de órdenes y posiciones no
      cuadra y el monitor se detuvo.

    Más uno elegido por el usuario o el asistente por defecto:
    - `PAUSA_DRAWDOWN`: el sistema se pausó por llegar al drawdown máximo.

    Y uno de la recuperación al arrancar (Paso 11c, decisión del 2026-10-10):
    - `CICLO_DIARIO_PERDIDO`: el scheduler arrancó después de la hora
      programada del ciclo diario y no existe un ciclo completo de hoy
      (típicamente porque el proceso murió a mitad o estuvo apagado). Solo
      avisa: NO relanza el ciclo (gastaría IA sin que el usuario lo pida).

    Y uno del tope de gasto de IA (decisión del 2026-10-10):
    - `PRESUPUESTO_IA_AGOTADO`: el gasto de IA de hoy (UTC) alcanzó el tope
      diario; no se inician más ciclos automáticos con IA hasta el día
      siguiente. Un aviso por día. No afecta al monitor ni a los stops.

    Y uno de la guardia de saldo de IA (decisión del 2026-10-11):
    - `SALDO_IA_INSUFICIENTE`: el saldo estimado de Anthropic no alcanza para un
      ciclo; no se inician ciclos automáticos con IA. Un aviso por día UTC.

    Y uno de la verificación previa de la llave de IA (decisión del 2026-10-30, tras el
    incidente de la llave vencida):
    - `LLAVE_IA_RECHAZADA`: Anthropic rechazó la llave (401/403: vencida, revocada o
      inválida) al verificarla antes de iniciar un ciclo; no se inician ciclos automáticos
      con IA. Un aviso por día UTC. No afecta al monitor ni a los stops.

    Y dos del resumen de cada ciclo de análisis (decisión del 2026-10-11):
    - `RESUMEN_CICLO`: el ciclo terminó; cuenta candidatos, propuestas, costo y
      gasto de IA del día. Uno por ciclo. Solo informa.
    - `CICLO_FALLIDO`: el ciclo no terminó (solo el nombre del error, nunca su
      texto). Solo informa.

    Y uno de la apertura automática de posiciones paper (decisión del 2026-10-12):
    - `POSICION_ABIERTA`: se abrió una posición paper (de la estrategia, de
      observación, o una entrada que aprobaste). Uno por posición, con unidades,
      precio, total de compra, stop y objetivo. Solo informa.

    Se guarda como VARCHAR validado (`native_enum=False`), igual que el resto
    de enums."""

    CAIDA_1H = "CAIDA_1H"
    VOLUMEN_ATIPICO = "VOLUMEN_ATIPICO"
    ORDEN_PENDIENTE_APROBACION = "ORDEN_PENDIENTE_APROBACION"
    BINANCE_CAIDO = "BINANCE_CAIDO"
    BINANCE_RESTABLECIDO = "BINANCE_RESTABLECIDO"
    DISCREPANCIA_RECONCILIACION = "DISCREPANCIA_RECONCILIACION"
    PAUSA_DRAWDOWN = "PAUSA_DRAWDOWN"
    CICLO_DIARIO_PERDIDO = "CICLO_DIARIO_PERDIDO"
    PRESUPUESTO_IA_AGOTADO = "PRESUPUESTO_IA_AGOTADO"
    SALDO_IA_INSUFICIENTE = "SALDO_IA_INSUFICIENTE"
    LLAVE_IA_RECHAZADA = "LLAVE_IA_RECHAZADA"
    RESUMEN_CICLO = "RESUMEN_CICLO"
    CICLO_FALLIDO = "CICLO_FALLIDO"
    POSICION_ABIERTA = "POSICION_ABIERTA"


class OrigenObservacionEquity(str, Enum):
    """Quién escribió una fila de `equity_observaciones`.

    - `MONITOR`: un chequeo normal del monitor (cada 60 s).
    - `REANUDACION`: el usuario reanudó a mano tras una pausa por drawdown; la
      fila fija el pico de equity nuevo igual al equity de ese momento.

    Se guarda como VARCHAR validado (`native_enum=False`), igual que el resto
    de enums."""

    MONITOR = "MONITOR"
    REANUDACION = "REANUDACION"


class EstadoSnapshot(str, Enum):
    """`trade_snapshots.system_state` (sección 8.5 del plan). `SCHEDULED` es
    el snapshot de rutina de cada chequeo de 60 s; el resto son eventos.

    Los que el sistema escribe HOY: `SCHEDULED`, `ENTRY`,
    `PARTIAL_TAKE_PROFIT`, `TRAILING_ACTIVATED`, `TRAILING_UPDATED`,
    `SYSTEM_PAUSED`, `RISK_ALERT` (Paso 11a-2: un aviso de caída o de volumen
    sobre la posición) y `EXIT`. `TARGET_REACHED` coincide siempre con
    `PARTIAL_TAKE_PROFIT` (en v1 el objetivo ES el TP parcial) y no se
    escribe aparte. `STOP_UPDATED` (el stop no se mueve en v1; el trailing
    se refleja en `TRAILING_UPDATED`) y `RECONCILIATION` (el monitor se
    detiene sin precios ante una discrepancia) existen como valor válido pero
    ningún código los emite todavía.

    Se guarda como VARCHAR validado (`native_enum=False`)."""

    SCHEDULED = "SCHEDULED"
    ENTRY = "ENTRY"
    STOP_UPDATED = "STOP_UPDATED"
    TARGET_REACHED = "TARGET_REACHED"
    PARTIAL_TAKE_PROFIT = "PARTIAL_TAKE_PROFIT"
    TRAILING_ACTIVATED = "TRAILING_ACTIVATED"
    TRAILING_UPDATED = "TRAILING_UPDATED"
    RISK_ALERT = "RISK_ALERT"
    SYSTEM_PAUSED = "SYSTEM_PAUSED"
    EXIT = "EXIT"
    RECONCILIATION = "RECONCILIATION"


class EstadoTesis(str, Enum):
    """`trade_results.thesis_status` (sección 8.4 del plan). Lo asigna una
    regla determinista por desenlace (decisión del usuario, 2026-10-05); el
    LLM no interviene. Ver `app.domain.rules.results.classify_thesis`."""

    CONFIRMED = "CONFIRMED"
    PARTIALLY_CONFIRMED = "PARTIALLY_CONFIRMED"
    INVALIDATED = "INVALIDATED"
    INCONCLUSIVE = "INCONCLUSIVE"


class AlcanceMetricas(str, Enum):
    """`performance_metrics.scope_type` (sección 8.6 del plan).

    - `GLOBAL`: todas las operaciones del periodo (`scope_key` NULL).
    - `SYMBOL`: las de un activo (`scope_key` = símbolo).
    - `SCORE_TERCIL`: las de un tercil del score inicial (`scope_key` =
      `SUPERIOR`, `MEDIO` o `INFERIOR`).

    El alcance "estrategia" del plan no existe todavía: no hay versiones de
    estrategia registradas. Se agrega cuando existan."""

    GLOBAL = "GLOBAL"
    SYMBOL = "SYMBOL"
    SCORE_TERCIL = "SCORE_TERCIL"


class ResultadoOperacion(str, Enum):
    """Clasificación de una operación cerrada por el signo de su P&L neto
    (la misma que usa `calculate_performance` para ganadoras, perdedoras y
    equilibrio). Solo para filtrar y etiquetar; no se guarda en la base."""

    GANADORA = "GANADORA"
    PERDEDORA = "PERDEDORA"
    EQUILIBRIO = "EQUILIBRIO"
