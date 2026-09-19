"""Test de integración: idempotencia real de `save_indicators` contra
Postgres. Mismo patrón que `tests/integration/test_candles_repository.py`.
"""

from datetime import datetime, timedelta, timezone
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


def test_save_indicators_batches_large_inserts_under_postgres_param_limit(db_session):
    """Regresión del crash real: `calculate_indicators_for_symbol_timeframe`
    le pasa a `save_indicators` TODO el historial de una combinación
    símbolo/temporalidad de una sola vez (para `1h` sobre ~2 años, más de
    100.000 filas), y un único `INSERT` sin trocear supera el límite duro
    de Postgres de 65.535 parámetros bind por consulta (5 columnas x fila
    => más de 13.107 filas ya lo revienta).

    20.000 filas (100.000 parámetros) es más que suficiente para disparar
    el bug si `save_indicators` dejara de trocear: reproduce el tamaño real
    que causó el error sin necesitar 17.500 velas reales de fixture.
    """
    rows = [
        _row(datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i), indicator_name="RSI_14")
        for i in range(20_000)
    ]

    inserted = save_indicators(db_session, rows)
    assert inserted == 20_000

    # Idempotencia también debe sobrevivir al troceo: cada lote hace su
    # propio ON CONFLICT DO NOTHING, así que reinsertar todo no debe
    # duplicar nada, sin importar en qué lote haya caído cada fila.
    reinserted = save_indicators(db_session, rows)
    assert reinserted == 0
