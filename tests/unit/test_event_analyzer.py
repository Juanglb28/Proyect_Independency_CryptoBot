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
    # +30 (día 50) se sale del historial (solo hay velas hasta el día 39):
    # se omite, no se rellena con el cierre más reciente disponible — ver
    # test_window_beyond_the_last_available_candle_is_declared_missing.
    assert set(by_offset) == set(WINDOW_OFFSETS_DAYS) - {30}
    # referencia: día 20, close=120
    assert by_offset[-7].reference_price == D(120)
    assert by_offset[-7].window_price == D(113)  # día 13
    assert by_offset[-7].price_change_pct == (D(113) - D(120)) / D(120) * D(100)

    assert by_offset[-1].window_price == D(119)
    assert by_offset[1].window_price == D(121)
    assert by_offset[3].window_price == D(123)
    assert by_offset[7].window_price == D(127)


def test_window_beyond_the_last_available_candle_is_declared_missing():
    """Bug real (2026-09-30): un evento reciente puede tener una ventana
    (p. ej. +30 días) cuya fecha todavía no tiene ninguna vela real
    ingerida. Antes del fix, `_closest_close_on_or_before` reutilizaba en
    silencio la vela más reciente disponible como si fuera el precio de esa
    fecha futura — el Agente 3, en una corrida real, detectó esto como una
    anomalía (dos ventanas distintas con el mismo cambio porcentual)."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles = _daily_candles("BTCUSDT", start, n=40)  # días 0..39
    event_date = (start + timedelta(days=20)).date()  # +30 -> día 50, fuera de rango

    rows = calculate_event_analysis(uuid.uuid4(), event_date, "BTCUSDT", candles)

    assert 30 not in {r.window_offset_days for r in rows}


def test_two_different_windows_never_collapse_to_the_same_future_candle():
    """Antes del fix, dos ventanas cuya fecha objetivo cae después de la
    última vela disponible (p. ej. +7 y +30, con solo 25 días de historial
    tras el evento) resolvían ambas a la MISMA vela más reciente, dando el
    mismo price_change_pct para las dos — exactamente la anomalía real que
    detectó el Agente 3. Ahora ambas se omiten."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles = _daily_candles("BTCUSDT", start, n=26)  # días 0..25
    event_date = (start + timedelta(days=20)).date()  # +7 -> día 27, +30 -> día 50

    rows = calculate_event_analysis(uuid.uuid4(), event_date, "BTCUSDT", candles)

    offsets_presentes = {r.window_offset_days for r in rows}
    assert 7 not in offsets_presentes
    assert 30 not in offsets_presentes


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
