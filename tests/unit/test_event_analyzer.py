"""Tests de app.data.event_analyzer.

Valores esperados calculados a mano (ver comentarios), igual criterio que
tests/unit/test_indicator_calculator.py: series de cierres simples y
lineales (100 + i) para que el porcentaje de cambio de cada ventana se
pueda verificar sin depender de una fuente externa.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.data.exchange_client import Candle
from app.data.event_analyzer import (
    WINDOW_OFFSETS_DAYS,
    calculate_event_analysis,
    resolve_scope_symbols,
)
from app.domain.symbols import ALLOWED_SYMBOLS


def D(x) -> Decimal:
    return Decimal(str(x))


def _daily_candles(symbol: str, start: datetime, n: int) -> list[Candle]:
    return [
        Candle(
            symbol=symbol,
            timeframe="1d",
            open_time=start + timedelta(days=i),
            open=D(100 + i),
            high=D(100 + i + 1),
            low=D(100 + i - 1),
            close=D(100 + i),
            volume=D(1000),
        )
        for i in range(n)
    ]


# --- resolve_scope_symbols -------------------------------------------------


def test_scope_none_or_empty_means_all_symbols():
    assert resolve_scope_symbols(None) == ALLOWED_SYMBOLS
    assert resolve_scope_symbols("") == ALLOWED_SYMBOLS
    assert resolve_scope_symbols("   ") == ALLOWED_SYMBOLS


def test_scope_global_case_insensitive_means_all_symbols():
    assert resolve_scope_symbols("GLOBAL") == ALLOWED_SYMBOLS
    assert resolve_scope_symbols("global") == ALLOWED_SYMBOLS


def test_scope_single_symbol():
    assert resolve_scope_symbols("ETHUSDT") == ("ETHUSDT",)


def test_scope_multiple_symbols_comma_separated_trims_and_upcases():
    assert resolve_scope_symbols(" btcusdt , ethusdt ") == ("BTCUSDT", "ETHUSDT")


def test_scope_with_symbol_outside_whitelist_raises():
    with pytest.raises(ValueError, match="no autorizados"):
        resolve_scope_symbols("FAKECOIN")


# --- calculate_event_analysis ----------------------------------------------


def test_calculate_event_analysis_matches_hand_computed_percentages():
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles = _daily_candles("BTCUSDT", start, n=40)
    event_date = (start + timedelta(days=20)).date()  # close = 120

    rows = calculate_event_analysis(uuid.uuid4(), event_date, "BTCUSDT", candles)

    by_offset = {r.window_offset_days: r for r in rows}
    assert set(by_offset) == set(WINDOW_OFFSETS_DAYS)
    # referencia: día 20, close=120
    assert by_offset[-7].reference_price == D(120)
    assert by_offset[-7].window_price == D(113)  # día 13
    assert by_offset[-7].price_change_pct == (D(113) - D(120)) / D(120) * D(100)

    assert by_offset[-1].window_price == D(119)
    assert by_offset[1].window_price == D(121)
    assert by_offset[3].window_price == D(123)
    assert by_offset[7].window_price == D(127)

    # +30 se sale del historial (solo hay 40 días, día 0..39): usa el cierre
    # más reciente disponible (día 39), no lo inventa ni lo omite.
    assert by_offset[30].window_date == (start + timedelta(days=39)).date()
    assert by_offset[30].window_price == D(139)


def test_event_older_than_all_available_candles_is_declared_missing():
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles = _daily_candles("BTCUSDT", start, n=40)
    event_date = (start - timedelta(days=5)).date()  # antes de cualquier vela

    rows = calculate_event_analysis(uuid.uuid4(), event_date, "BTCUSDT", candles)

    assert rows == []


def test_sparse_history_never_fabricates_a_zero_percent_row():
    """Solo dos velas separadas por 39 días: ninguna ventana (-7..+30) cae
    exactamente sobre la segunda vela, así que toda ventana positiva
    resolvería al mismo día que la referencia — eso debe omitirse, no
    reportarse como 0% de cambio."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles = [
        Candle(
            symbol="BTCUSDT", timeframe="1d", open_time=start,
            open=D(100), high=D(101), low=D(99), close=D(100), volume=D(1000),
        ),
        Candle(
            symbol="BTCUSDT", timeframe="1d", open_time=start + timedelta(days=39),
            open=D(200), high=D(201), low=D(199), close=D(200), volume=D(1000),
        ),
    ]
    event_date = start.date()

    rows = calculate_event_analysis(uuid.uuid4(), event_date, "BTCUSDT", candles)

    assert rows == []


def test_rejects_candles_of_a_different_symbol_or_timeframe():
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles = _daily_candles("ETHUSDT", start, n=40)

    with pytest.raises(ValueError, match="no son todas diarias"):
        calculate_event_analysis(uuid.uuid4(), start.date(), "BTCUSDT", candles)


def test_empty_candles_returns_empty_list():
    assert calculate_event_analysis(uuid.uuid4(), datetime(2024, 1, 1).date(), "BTCUSDT", []) == []
