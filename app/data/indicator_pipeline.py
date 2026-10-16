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

# Cola para el cálculo INCREMENTAL de indicadores (ciclo horario, 2026-10-12).
#
# El ciclo horario solo agrega la(s) vela(s) recién cerrada(s); recalcular toda
# la historia cada hora era el cuello de botella. Con una cola de velas basta,
# siempre que traiga suficiente CALENTAMIENTO para que cada indicador de la vela
# nueva salga idéntico al de historia completa:
#   - SMA 180 (la ventana más larga) y distancia 90 d: ventana fija → bastan 180.
#   - RSI/ATR de Wilder (suavizado recursivo, factor 13/14): el peso de la semilla
#     decae como (13/14)^k; para 12 decimales (NUMERIC(30,12)) bastan ~382 velas.
# 1000 deja un margen enorme ((13/14)^1000 ≈ 1e-33). Un test comprueba que, con
# esta cola, los indicadores de las velas nuevas coinciden EXACTO con los de
# historia completa. Las velas de la cola que ya existen no se reescriben
# (`save_indicators` usa ON CONFLICT DO NOTHING), así que la parte "fría" de la
# cola —donde el RSI/ATR aún no convergió— nunca pisa los valores autoritativos.
#
# Por qué es seguro que las velas NUEVAS caigan siempre en la zona caliente: el
# ciclo horario hace backfill de solo 3 días (`hourly_cycle._BACKFILL_WINDOW_DAYS`),
# así que como mucho añade ~72 velas 1h de una vez; su calentamiento es
# 1000 - 72 = 928 velas, muy por encima de las ~382 que exige la convergencia a
# 12 decimales. El ciclo diario (00:05) recalcula igual toda la historia.
VELAS_DE_COLA_INCREMENTAL = 1000


@dataclass(frozen=True)
class IndicatorCalculationResult:
    """Resumen de calcular los indicadores de un (symbol, timeframe)."""

    symbol: str
    timeframe: str
    candles_used: int
    calculated: int
    inserted: int


def calculate_indicators_for_symbol_timeframe(
    session: Session, symbol: str, timeframe: str, *, tail: int | None = None
) -> IndicatorCalculationResult:
    """Lee las velas ya guardadas de `symbol`/`timeframe`, calcula sus
    indicadores y los persiste.

    `tail`: si se da, calcula solo sobre las últimas `tail` velas (cálculo
    INCREMENTAL del ciclo horario). Los indicadores de las velas realmente
    nuevas salen idénticos a los de historia completa (ver
    `VELAS_DE_COLA_INCREMENTAL`); las ya guardadas no se reescriben. `None`
    (por defecto) calcula sobre toda la historia — lo que necesita el ciclo
    diario, que además recalcula la distancia ATH (máximo expansivo sobre toda
    la serie).

    Si no hay velas guardadas (símbolo/temporalidad sin backfill todavía),
    no es un error: se declara el dato faltante devolviendo un resultado con
    `candles_used=0` en vez de intentar calcular sobre una lista vacía o
    lanzar una excepción (sección P3 del plan).
    """
    candles = get_candles(session, symbol, timeframe, tail=tail)
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
    session: Session, symbols: Iterable[str], timeframes: Iterable[str], *, tail: int | None = None
) -> list[IndicatorCalculationResult]:
    """Corre `calculate_indicators_for_symbol_timeframe` para cada
    combinación, haciendo `commit()` después de cada una — no al final de
    todas — para que una interrupción a mitad de camino no pierda el
    progreso ya guardado (P5 del plan: mismo patrón que
    `candle_ingestion.py::backfill_all`).

    `tail`: se pasa tal cual a cada combinación. El ciclo horario usa
    `VELAS_DE_COLA_INCREMENTAL` (cálculo incremental); el diario deja `None`
    (historia completa).
    """
    results: list[IndicatorCalculationResult] = []
    for symbol in symbols:
        for timeframe in timeframes:
            result = calculate_indicators_for_symbol_timeframe(session, symbol, timeframe, tail=tail)
            results.append(result)
            session.commit()
    return results
