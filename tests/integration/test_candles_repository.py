"""Test de integración: idempotencia real de `save_candles` contra Postgres.

Complementa tests/integration/test_schema_idempotency.py (que prueba la
restricción única a nivel de una sola inserción vía ORM) probando el
camino real de `save_candles`: inserción masiva con
`ON CONFLICT DO NOTHING`.
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.data.exchange_client import Candle
from app.repositories.candles import save_candles


def _candle(open_time: datetime, **overrides) -> Candle:
    base = Candle(
        symbol="BTCUSDT",
        timeframe="1d",
        open_time=open_time,
        open=Decimal("42000.5"),
        high=Decimal("43000.0"),
        low=Decimal("41500.0"),
        close=Decimal("42800.25"),
        volume=Decimal("1234.5"),
    )
    base.update(overrides)
    return base


def test_save_candles_inserts_new_rows(db_session):
    candles = [
        _candle(datetime(2024, 1, 1, tzinfo=timezone.utc)),
        _candle(datetime(2024, 1, 2, tzinfo=timezone.utc)),
    ]

    inserted = save_candles(db_session, candles)

    assert inserted == 2


def test_save_candles_is_idempotent_on_rerun(db_session):
    """Guardar el mismo lote dos veces (simulando un reintento tras un
    corte a mitad de una ingesta) no debe duplicar ni fallar — sección
    15.7 y P5 del plan."""
    candles = [
        _candle(datetime(2024, 2, 1, tzinfo=timezone.utc)),
        _candle(datetime(2024, 2, 2, tzinfo=timezone.utc)),
    ]

    first = save_candles(db_session, candles)
    second = save_candles(db_session, candles)

    assert first == 2
    assert second == 0  # ya estaban todas guardadas, ninguna fila nueva


def test_save_candles_inserts_only_the_new_ones_in_a_mixed_batch(db_session):
    """Un lote con algunas velas ya guardadas y otras nuevas debe insertar
    solo las nuevas, sin fallar por las que ya existían."""
    already_saved = _candle(datetime(2024, 3, 1, tzinfo=timezone.utc))
    save_candles(db_session, [already_saved])

    mixed_batch = [
        already_saved,
        _candle(datetime(2024, 3, 2, tzinfo=timezone.utc)),
    ]
    inserted = save_candles(db_session, mixed_batch)

    assert inserted == 1


def test_save_candles_with_empty_list_is_a_no_op(db_session):
    assert save_candles(db_session, []) == 0
