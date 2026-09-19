"""Test de integración: idempotencia real de `save_indicators` contra
Postgres. Mismo patrón que `tests/integration/test_candles_repository.py`.
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.data.indicator_calculator import IndicatorRow
from app.repositories.indicators import save_indicators


def _row(calculated_at: datetime, **overrides) -> IndicatorRow:
    defaults = dict(
        symbol="BTCUSDT",
        timeframe="1d",
        calculated_at=calculated_at,
        indicator_name="RSI_14",
        value=Decimal("55.5"),
    )
    defaults.update(overrides)
    return IndicatorRow(**defaults)


def test_save_indicators_inserts_new_rows(db_session):
    rows = [
        _row(datetime(2024, 1, 1, tzinfo=timezone.utc)),
        _row(datetime(2024, 1, 2, tzinfo=timezone.utc)),
    ]

    inserted = save_indicators(db_session, rows)

    assert inserted == 2


def test_save_indicators_is_idempotent_on_rerun(db_session):
    rows = [
        _row(datetime(2024, 2, 1, tzinfo=timezone.utc)),
        _row(datetime(2024, 2, 2, tzinfo=timezone.utc)),
    ]

    first = save_indicators(db_session, rows)
    second = save_indicators(db_session, rows)

    assert first == 2
    assert second == 0


def test_save_indicators_distinguishes_by_indicator_name(db_session):
    """Misma vela (symbol+timeframe+calculated_at), dos indicadores
    distintos: ambos deben guardarse — el nombre del indicador es parte de
    la clave única."""
    calculated_at = datetime(2024, 3, 1, tzinfo=timezone.utc)
    rows = [
        _row(calculated_at, indicator_name="RSI_14", value=Decimal("60")),
        _row(calculated_at, indicator_name="ATR_14", value=Decimal("120.5")),
    ]

    inserted = save_indicators(db_session, rows)

    assert inserted == 2


def test_save_indicators_with_empty_list_is_a_no_op(db_session):
    assert save_indicators(db_session, []) == 0
