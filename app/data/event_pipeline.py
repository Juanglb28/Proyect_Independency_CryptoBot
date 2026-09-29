"""Orquesta el cálculo de `analisis_eventos` sobre todo el catálogo y su
persistencia (sección 15.10 del plan, Paso 5).

Mismo rol que `indicator_pipeline.py` cumple para los indicadores: separa
"calcular" (puro, sin base de datos — `event_analyzer.py`) de "leer
eventos/velas de la base de datos, calcular y guardar para todo el
catálogo, con resiliencia ante un corte a mitad de camino" (aquí).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.data.event_analyzer import calculate_event_analysis, resolve_scope_symbols
from app.repositories.candles import get_candles
from app.repositories.event_analysis import save_event_analysis
from app.repositories.events import get_events

_DAILY_TIMEFRAME = "1d"


@dataclass(frozen=True)
class EventAnalysisResult:
    """Resumen de calcular `analisis_eventos` de un evento."""

    external_id: str
    symbols_evaluated: int
    symbols_sin_velas: tuple[str, ...]
    calculated: int
    inserted: int


def calculate_event_analysis_for_event(session: Session, evento) -> EventAnalysisResult:
    """Calcula y guarda `analisis_eventos` de un único evento, para todos
    los símbolos que indique su `scope` (sección 6 del plan).

    Un símbolo sin velas diarias guardadas todavía no es un error: se
    declara "sin velas" y se sigue con el resto (mismo criterio que
    `calculate_indicators_for_symbol_timeframe` — sección P3 del plan).
    """
    symbols = resolve_scope_symbols(evento.scope)

    calculated_rows = []
    symbols_sin_velas: list[str] = []
    for symbol in symbols:
        candles = get_candles(session, symbol, _DAILY_TIMEFRAME)
        if not candles:
            symbols_sin_velas.append(symbol)
            continue
        calculated_rows.extend(calculate_event_analysis(evento.id, evento.event_date, symbol, candles))

    inserted = save_event_analysis(session, calculated_rows)

    return EventAnalysisResult(
        external_id=evento.external_id,
        symbols_evaluated=len(symbols),
        symbols_sin_velas=tuple(symbols_sin_velas),
        calculated=len(calculated_rows),
        inserted=inserted,
    )


def calculate_event_analysis_for_all(session: Session) -> list[EventAnalysisResult]:
    """Corre `calculate_event_analysis_for_event` para todo el catálogo,
    haciendo `commit()` después de cada evento — no al final de todos —
    para que un corte a mitad de camino no pierda el progreso ya guardado
    (P5 del plan, mismo patrón que `indicator_pipeline.py::
    calculate_indicators_for_all`)."""
    results: list[EventAnalysisResult] = []
    for evento in get_events(session):
        result = calculate_event_analysis_for_event(session, evento)
        results.append(result)
        session.commit()
    return results
