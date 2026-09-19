"""Orquesta el cálculo de indicadores sobre muchas combinaciones
símbolo/temporalidad y su persistencia (sección 15.10 del plan, Paso 4).

Mismo rol que `candle_ingestion.py` cumple para las velas: separa "calcular"
(puro, sin base de datos — `indicator_calculator.py`) de "leer de la base de
datos, calcular y guardar para muchas combinaciones, con resiliencia ante
un corte a mitad de camino" (aquí).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.data.indicator_calculator import calculate_indicators
from app.repositories.candles import get_candles
from app.repositories.indicators import save_indicators


@dataclass(frozen=True)
class IndicatorCalculationResult:
    """Resumen de calcular los indicadores de un (symbol, timeframe)."""

    symbol: str
    timeframe: str
    candles_used: int
    calculated: int
    inserted: int


def calculate_indicators_for_symbol_timeframe(
    session: Session, symbol: str, timeframe: str
) -> IndicatorCalculationResult:
    """Lee las velas ya guardadas de `symbol`/`timeframe`, calcula sus
    indicadores y los persiste.

    Si no hay velas guardadas (símbolo/temporalidad sin backfill todavía),
    no es un error: se declara el dato faltante devolviendo un resultado con
    `candles_used=0` en vez de intentar calcular sobre una lista vacía o
    lanzar una excepción (sección P3 del plan).
    """
    candles = get_candles(session, symbol, timeframe)
    if not candles:
        return IndicatorCalculationResult(symbol, timeframe, candles_used=0, calculated=0, inserted=0)

    rows = calculate_indicators(symbol, timeframe, candles)
    inserted = save_indicators(session, rows)
    return IndicatorCalculationResult(
        symbol=symbol,
        timeframe=timeframe,
        candles_used=len(candles),
        calculated=len(rows),
        inserted=inserted,
    )


def calculate_indicators_for_all(
    session: Session, symbols: Iterable[str], timeframes: Iterable[str]
) -> list[IndicatorCalculationResult]:
    """Corre `calculate_indicators_for_symbol_timeframe` para cada
    combinación, haciendo `commit()` después de cada una — no al final de
    todas — para que una interrupción a mitad de camino no pierda el
    progreso ya guardado (P5 del plan: mismo patrón que
    `candle_ingestion.py::backfill_all`).
    """
    results: list[IndicatorCalculationResult] = []
    for symbol in symbols:
        for timeframe in timeframes:
            result = calculate_indicators_for_symbol_timeframe(session, symbol, timeframe)
            results.append(result)
            session.commit()
    return results
