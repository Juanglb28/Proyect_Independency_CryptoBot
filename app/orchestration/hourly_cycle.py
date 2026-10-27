"""Ciclo horario: ingesta incremental, indicadores y detección de candidatos
nuevos (enmienda E3, Paso 11b del plan).

Cada hora:

1. Ingesta de velas para las temporalidades que cierran a esta hora.
2. Cálculo de indicadores para esas temporalidades.
3. Ejecución del pre-filtro determinista (siempre sobre velas diarias).
4. Comparación con el último ciclo exitoso para detectar símbolos NUEVOS
   por encima del umbral 45.

Si hay candidatos nuevos, el resultado lo indica para que el scheduler
decida si dispara un ciclo de análisis IA (`run_daily_cycle`).

No crea ``ciclo_analisis`` propio — es un mecanismo de detección y
mantenimiento de datos, no un ciclo de análisis. El ciclo de análisis
IA, si se dispara, crea su propio ``ciclo_analisis`` y persiste su
propio pre-filtro, estableciendo la nueva referencia para la próxima
detección horaria.

Temporalidades y horas de cierre
----------------------------------
Binance usa 00:00 UTC como inicio del período para todas las
temporalidades. A cada hora cierra la vela 1h; a horas múltiplo de 4
cierra también la de 4h; a las 00:00 y 12:00 la de 12h; y solo a las
00:00 la diaria. El ciclo horario ingesta SOLO las temporalidades que
acaban de cerrar a esa hora — no tiene sentido descargar velas 4h cuando
estamos en la hora 3 (la vela no cambió desde la hora 0). Los indicadores
se recalculan para esas mismas temporalidades.

Refresco de velas de las 9:30 (Colombia) — 2026-10-17
------------------------------------------------------
El ciclo horario solo descarga las temporalidades que cierran a esa hora. Si el
equipo está apagado a las 12:05 UTC (7:05 a. m. en Colombia), la vela de 12 h (y
la de 4 h de las 12:00) queda guardada a medias hasta el ciclo diario de las
00:05 UTC. ``run_candle_refresh`` es un job aparte, a las 14:30 UTC (9:30 a. m.),
que solo repasa 1h/4h/12h: ingesta + indicadores. NO corre el pre-filtro ni
detecta candidatos ni dispara IA: no altera la enmienda E3.

Recuperación de velas vencidas al arrancar — 2026-10-17
--------------------------------------------------------
Si el scheduler se (re)inicia con velas guardadas a medias cuya hora de cierre ya
pasó, ``run_stale_candle_catchup`` las vuelve a descargar y recalcula los
indicadores de esas series (mismo camino de código que
``scripts/repair_partial_candles.py --ejecutar``). Solo velas e indicadores: sin
pre-filtro, sin candidatos, sin IA. Cada serie va en su propia transacción: un
fallo de red en una no pierde lo ya reparado.

Decisiones que implementa
--------------------------
- E3 (aprobada 2026-10-09): ingesta horaria + pre-filtro determinista,
  IA solo para candidatos nuevos.
- D4 (confirmada 2026-10-09): la notificación de entradas pendientes la
  maneja el scheduler, no este módulo.
- El umbral de detección es ``SCORE_MINIMO_PREFILTRO`` (45), idéntico al
  del pre-filtro diario (decisión del usuario: "símbolo NUEVO sobre
  umbral 45 que NO era candidato en el último ciclo diario").
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.data.candle_ingestion import BackfillResult, backfill_all, velas_vencidas_sin_cerrar
from app.data.candle_repair import reparar_serie
from app.data.exchange_client import get_exchange
from app.data.indicator_pipeline import (
    VELAS_DE_COLA_INCREMENTAL,
    IndicatorCalculationResult,
    calculate_indicators_for_all,
)
from app.data.prefilter_pipeline import PrefilterRunResult, run_prefilter
from app.domain.rules.scoring import SCORE_MINIMO_PREFILTRO
from app.db.session import session_scope
from app.domain.symbols import ALLOWED_SYMBOLS
from app.repositories.analysis import list_ciclos_recientes, list_prefiltro_de_ciclo
from app.repositories.simbolos_ocupados import simbolos_ocupados

logger = logging.getLogger(__name__)

# Ventana de ingesta para el ciclo horario: 3 días atrás es suficiente
# para cubrir huecos por caídas breves sin repetir el backfill completo de
# 2 años. La ingesta es idempotente: velas ya guardadas se ignoran.
_BACKFILL_WINDOW_DAYS = 3

REFRESCO_VELAS_HORA_UTC = time(14, 30)
"""Hora (UTC) del refresco de velas: 9:30 a. m. en Colombia (UTC-5, sin horario de
verano). Es la ÚNICA definición: el scheduler la usa para su disparador."""

REFRESCO_VELAS_TEMPORALIDADES: tuple[str, ...] = ("1h", "4h", "12h")
"""Temporalidades que repasa el refresco. La diaria no: solo cambia a las 00:00 UTC
y la refresca el ciclo diario de las 00:05."""


# ---------------------------------------------------------------------------
# Temporalidades por hora
# ---------------------------------------------------------------------------


def timeframes_que_cierran_a_esta_hora(hour_utc: int) -> tuple[str, ...]:
    """Temporalidades cuya vela cierra a la hora UTC dada.

    - 1h: siempre (cada hora cierra una vela).
    - 4h: horas múltiplo de 4 (0, 4, 8, 12, 16, 20).
    - 12h: horas múltiplo de 12 (0, 12).
    - 1d: solo a las 00:00 UTC (cierre del día).

    Pública porque ``scheduler.py`` la usa para decidir si a la hora 0 se
    salta el ciclo horario (el diario a las 00:05 ya cubre todo).
    """
    if hour_utc < 0 or hour_utc > 23:
        raise ValueError(f"hour_utc debe estar entre 0 y 23, recibido {hour_utc}")
    tfs: list[str] = ["1h"]
    if hour_utc % 4 == 0:
        tfs.append("4h")
    if hour_utc % 12 == 0:
        tfs.append("12h")
    if hour_utc == 0:
        tfs.append("1d")
    return tuple(tfs)


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NewCandidate:
    """Símbolo detectado como NUEVO candidato por el ciclo horario."""

    symbol: str
    score_actual: Decimal
    score_en_ultimo_ciclo: Decimal | None
    """Score en el último ciclo exitoso, o ``None`` si no tenía entrada."""
    estado_en_ultimo_ciclo: str | None
    """Estado (``EstadoPrefiltro``) en el último ciclo, o ``None``."""


@dataclass(frozen=True)
class HourlyCycleResult:
    """Resumen de una corrida del ciclo horario."""

    hora_utc: int
    timeframes_ingested: tuple[str, ...]
    backfill_results: tuple[BackfillResult, ...]
    indicator_results: tuple[IndicatorCalculationResult, ...]
    prefilter: PrefilterRunResult | None
    """``None`` si el pre-filtro no se corrió (p.ej. sin velas diarias)."""

    new_candidates: tuple[NewCandidate, ...]
    """Símbolos que cruzaron el umbral 45 y NO eran candidatos en el último
    ciclo exitoso."""

    ultimo_ciclo_id: uuid.UUID | None
    """ID del último ciclo exitoso usado como referencia, o ``None``."""

    elapsed_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Lógica interna
# ---------------------------------------------------------------------------


def _get_ultimo_ciclo_exitoso(
    session: Session,
) -> tuple[uuid.UUID | None, dict[str, Decimal | None]]:
    """Busca el último ciclo exitoso y devuelve ``(id, {symbol: score_final})``.

    Un ciclo es "exitoso" si tiene ``finished_at`` no nulo y
    ``error_message`` nulo. No distingue ciclos diarios de ciclos
    disparados por el hourly: la referencia es siempre "el último análisis
    completo que se hizo" — si el hourly disparó un ciclo IA hace 2 horas,
    esos resultados ya son la nueva referencia (así se evita re-disparar
    para los mismos símbolos).

    Si el ciclo exitoso no tiene filas en ``prefiltro_resultados`` (es un
    ciclo anterior a esa tabla) se trata como si no hubiera referencia
    (``scores`` vacío → la llamadora lo maneja como "sin ciclo previo").
    """
    ciclos = list_ciclos_recientes(session, limit=20)
    for ciclo in ciclos:
        if ciclo.finished_at is not None and ciclo.error_message is None:
            prefiltro_rows = list_prefiltro_de_ciclo(session, ciclo.id)
            if not prefiltro_rows:
                # Ciclo anterior a la tabla prefiltro_resultados (2026-10-07):
                # no se puede comparar — buscar otro más reciente sería
                # incorrecto (el ciclo intermedio sí corrió, solo que no
                # guardó pre-filtro). Tratar como "sin referencia".
                logger.warning(
                    "Ciclo exitoso %s no tiene prefiltro_resultados (anterior a 2026-10-07), "
                    "no se puede usar como referencia para detección horaria",
                    ciclo.id,
                    extra={"component": "orchestration.hourly_cycle"},
                )
                return None, {}
            scores: dict[str, Decimal | None] = {}
            for row in prefiltro_rows:
                scores[row.symbol] = row.score_final
            return ciclo.id, scores

    return None, {}


def _detectar_candidatos_nuevos(
    prefilter_result: PrefilterRunResult,
    scores_ultimo_ciclo: dict[str, Decimal | None],
    *,
    ocupados: frozenset[str] = frozenset(),
) -> list[NewCandidate]:
    """Compara scores actuales con los del último ciclo exitoso.

    Un símbolo es "nuevo" si:

    1. Su score actual ≥ ``SCORE_MINIMO_PREFILTRO`` (45), Y
    2. En el último ciclo NO era candidato (score < 45, o ``SIN_DATOS``,
       o no tenía entrada).

    Esto detecta símbolos que CRUZARON el umbral desde el último
    análisis — no los que ya estaban arriba y siguen arriba (esos ya
    fueron analizados por el ciclo que generó la referencia).

    Los símbolos `ocupados` (posición abierta o entrada pendiente) nunca son
    "nuevos": no se pueden analizar de nuevo ni entrar (decisión del 2026-10-26),
    así que no deben disparar un ciclo de IA.
    """
    nuevos: list[NewCandidate] = []
    umbral = SCORE_MINIMO_PREFILTRO

    for score in prefilter_result.scores:
        if score.score_final is None or score.score_final < umbral:
            continue
        if score.symbol in ocupados:
            continue

        # ¿Era candidato en el último ciclo?
        score_previo = scores_ultimo_ciclo.get(score.symbol)

        if score_previo is not None and score_previo >= umbral:
            continue  # ya era candidato, no es nuevo

        # Es nuevo: cruzó el umbral
        estado_previo: str | None = None
        if score.symbol in scores_ultimo_ciclo:
            if scores_ultimo_ciclo[score.symbol] is None:
                estado_previo = "SIN_DATOS"
            else:
                estado_previo = "DESCARTADO_POR_PUNTAJE"
        # Si no está en el dict → no tenía entrada en el último ciclo

        nuevos.append(
            NewCandidate(
                symbol=score.symbol,
                score_actual=score.score_final,
                score_en_ultimo_ciclo=score_previo,
                estado_en_ultimo_ciclo=estado_previo,
            )
        )

    return nuevos


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------


def run_hourly_cycle(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> HourlyCycleResult:
    """Ejecuta el ciclo horario completo.

    1. Ingesta de velas para las temporalidades aplicables.
    2. Cálculo de indicadores.
    3. Pre-filtro determinista.
    4. Detección de candidatos nuevos vs. último ciclo exitoso.

    No crea ``ciclo_analisis``. No llama a agentes IA. El scheduler
    decide si disparar ``run_daily_cycle`` cuando hay candidatos nuevos.

    ``settings`` se recibe por consistencia con ``run_daily_cycle`` (el
    scheduler pasa lo mismo a ambos), aunque hoy no se usa — la ingesta y
    los indicadores no necesitan credenciales (Binance mainnet, datos
    públicos).
    """
    start = datetime.now(timezone.utc)
    ahora = now if now is not None else start
    hora_utc = ahora.hour

    # 1. Temporalidades que cierran a esta hora
    timeframes = timeframes_que_cierran_a_esta_hora(hora_utc)
    logger.info(
        "Ciclo horario: hora=%d UTC, temporalidades=%s",
        hora_utc,
        timeframes,
        extra={"component": "orchestration.hourly_cycle", "hora_utc": hora_utc},
    )

    # 2. Ingesta de velas (ventana corta, idempotente)
    since = ahora - timedelta(days=_BACKFILL_WINDOW_DAYS)
    backfill_results = backfill_all(
        session, ALLOWED_SYMBOLS, timeframes, since, until=ahora, now=ahora
    )

    total_inserted = sum(r.inserted for r in backfill_results)
    total_updated = sum(r.updated for r in backfill_results)
    logger.info(
        "Ingesta horaria: %d velas nuevas, %d actualizadas",
        total_inserted,
        total_updated,
        extra={"component": "orchestration.hourly_cycle"},
    )

    # 3. Cálculo de indicadores para las temporalidades ingresadas.
    # INCREMENTAL: solo sobre la cola reciente (el ciclo horario agrega una vela
    # por temporalidad; recalcular toda la historia cada hora era el cuello de
    # botella). Los indicadores de las velas nuevas salen idénticos a los de
    # historia completa; el ciclo diario (00:05) sigue recalculando todo.
    indicator_results = calculate_indicators_for_all(
        session, ALLOWED_SYMBOLS, timeframes, tail=VELAS_DE_COLA_INCREMENTAL
    )

    total_indicators = sum(r.inserted for r in indicator_results)
    logger.info(
        "Indicadores horarios: %d nuevos calculados",
        total_indicators,
        extra={"component": "orchestration.hourly_cycle"},
    )

    # 4. Pre-filtro (siempre sobre velas diarias — run_prefilter lee de la DB)
    prefilter_result = run_prefilter(session)
    logger.info(
        "Pre-filtro horario: %d scores, %d candidatos, %d sin datos",
        len(prefilter_result.scores),
        len(prefilter_result.candidates),
        len(prefilter_result.symbols_sin_datos),
        extra={"component": "orchestration.hourly_cycle"},
    )

    # 5. Comparar con el último ciclo exitoso
    ultimo_id, scores_ultimo = _get_ultimo_ciclo_exitoso(session)

    if ultimo_id is None:
        logger.info(
            "Sin ciclo previo exitoso con prefiltro_resultados — "
            "no se puede detectar candidatos nuevos",
            extra={"component": "orchestration.hourly_cycle"},
        )
        new_candidates: list[NewCandidate] = []
    else:
        new_candidates = _detectar_candidatos_nuevos(
            prefilter_result, scores_ultimo, ocupados=simbolos_ocupados(session)
        )
        if new_candidates:
            logger.info(
                "Candidatos NUEVOS detectados: %s",
                [c.symbol for c in new_candidates],
                extra={
                    "component": "orchestration.hourly_cycle",
                    "ultimo_ciclo_id": str(ultimo_id),
                    "nuevos": [c.symbol for c in new_candidates],
                },
            )
        else:
            logger.info(
                "Sin candidatos nuevos (referencia: ciclo %s)",
                ultimo_id,
                extra={"component": "orchestration.hourly_cycle"},
            )

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()

    return HourlyCycleResult(
        hora_utc=hora_utc,
        timeframes_ingested=timeframes,
        backfill_results=tuple(backfill_results),
        indicator_results=tuple(indicator_results),
        prefilter=prefilter_result,
        new_candidates=tuple(new_candidates),
        ultimo_ciclo_id=ultimo_id,
        elapsed_seconds=elapsed,
    )


# ---------------------------------------------------------------------------
# Refresco de velas (job aparte, 14:30 UTC = 9:30 a. m. Colombia)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandleRefreshResult:
    """Resumen de un refresco de velas."""

    timeframes: tuple[str, ...]
    velas_nuevas: int
    velas_actualizadas: int
    indicadores_nuevos: int
    elapsed_seconds: float


def run_candle_refresh(
    session: Session,
    *,
    now: datetime | None = None,
    timeframes: tuple[str, ...] = REFRESCO_VELAS_TEMPORALIDADES,
) -> CandleRefreshResult:
    """Repasa las velas de ``timeframes`` (ventana de 3 días, idempotente) y
    recalcula sus indicadores de forma incremental.

    Cierra las velas que quedaron guardadas a medias porque un ciclo horario no
    corrió a su hora. No corre el pre-filtro, no detecta candidatos, no llama a
    la IA y no escribe en tablas operativas: solo velas e indicadores derivados.
    """
    inicio = datetime.now(timezone.utc)
    ahora = now if now is not None else inicio

    backfill_results = backfill_all(
        session, ALLOWED_SYMBOLS, timeframes, ahora - timedelta(days=_BACKFILL_WINDOW_DAYS), until=ahora, now=ahora
    )
    indicator_results = calculate_indicators_for_all(
        session, ALLOWED_SYMBOLS, timeframes, tail=VELAS_DE_COLA_INCREMENTAL
    )
    return CandleRefreshResult(
        timeframes=tuple(timeframes),
        velas_nuevas=sum(r.inserted for r in backfill_results),
        velas_actualizadas=sum(r.updated for r in backfill_results),
        indicadores_nuevos=sum(r.inserted for r in indicator_results),
        elapsed_seconds=(datetime.now(timezone.utc) - inicio).total_seconds(),
    )


# ---------------------------------------------------------------------------
# Recuperación de velas vencidas (al arrancar el scheduler)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaleCandleCatchupResult:
    """Resumen de la recuperación de velas vencidas sin cerrar."""

    series_con_velas_vencidas: int
    velas_reparadas: int
    series_fallidas: int
    velas_sin_reparar: int
    elapsed_seconds: float


def run_stale_candle_catchup(
    session_factory: sessionmaker[Session],
    *,
    now: datetime | None = None,
    exchange: object | None = None,
) -> StaleCandleCatchupResult:
    """Repara las velas guardadas a medias cuya hora de cierre ya pasó.

    Para cada serie (símbolo + temporalidad) con velas vencidas sin cerrar, vuelve
    a descargarlas desde la más antigua y recalcula los indicadores de esa serie
    (``reparar_serie``). Una transacción por serie: si Binance falla en una, se
    registra, se sigue con las demás y lo ya confirmado queda (P5).

    No corre el pre-filtro, no detecta candidatos, no llama a la IA y no escribe en
    tablas operativas. Solo atiende símbolos de la lista autorizada.
    """
    inicio = datetime.now(timezone.utc)
    ahora = now if now is not None else inicio

    with session_scope(session_factory) as session:
        pendientes = sorted(
            {(s, tf) for s, tf, _ in velas_vencidas_sin_cerrar(session, ahora=ahora) if s in ALLOWED_SYMBOLS}
        )
    if not pendientes:
        return StaleCandleCatchupResult(0, 0, 0, 0, (datetime.now(timezone.utc) - inicio).total_seconds())

    if exchange is None:
        exchange = get_exchange()

    reparadas = 0
    fallidas = 0
    sin_reparar = 0
    for symbol, timeframe in pendientes:
        try:
            with session_scope(session_factory) as session:
                resultado = reparar_serie(session, symbol, timeframe, ahora=ahora, exchange=exchange)
        except Exception:
            fallidas += 1
            logger.error(
                "Recuperación de velas: falló %s %s; se sigue con las demás series",
                symbol,
                timeframe,
                exc_info=True,
                extra={"component": "orchestration.hourly_cycle", "symbol": symbol, "timeframe": timeframe},
            )
            continue
        if resultado is None:
            continue
        reparadas += len(resultado.velas)
        sin_reparar += resultado.restantes

    return StaleCandleCatchupResult(
        series_con_velas_vencidas=len(pendientes),
        velas_reparadas=reparadas,
        series_fallidas=fallidas,
        velas_sin_reparar=sin_reparar,
        elapsed_seconds=(datetime.now(timezone.utc) - inicio).total_seconds(),
    )
