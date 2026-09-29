"""Test de integración de `verify_symbol_timeframe`/`verify_all` contra
Postgres real: guarda velas e indicadores de verdad (vía el mismo pipeline
que usa `scripts/calculate_indicators.py`) y verifica que el round-trip
completo (calcular → guardar → releer → comparar) da limpio, y que
corromper o borrar una fila persistida sí se detecta.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.data.exchange_client import Candle
from app.data.indicator_pipeline import calculate_indicators_for_symbol_timeframe
from app.data.indicator_verification import verify_symbol_timeframe
from app.db.models.indicators import Indicador
from app.repositories.candles import save_candles


def _candles(symbol: str, timeframe: str, n: int) -> list[Candle]:
    start = datetime(2024, 6, 1, tzinfo=timezone.utc)
    price = Decimal("50")
    out = []
    for i in range(n):
        price = price + Decimal(i % 4) - Decimal(1)
        out.append(
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=start + timedelta(days=i),
                open=price,
                high=price + Decimal(1),
                low=price - Decimal(1),
                close=price,
                volume=Decimal(50 + i),
            )
        )
    return out


def test_real_pipeline_round_trip_verifies_clean(db_session):
    save_candles(db_session, _candles("BTCUSDT", "1d", n=200))
    db_session.flush()
    calculate_indicators_for_symbol_timeframe(db_session, "BTCUSDT", "1d")
    db_session.flush()

    result = verify_symbol_timeframe(db_session, "BTCUSDT", "1d")

    assert result.candles_used == 200
    assert result.persisted_count > 0
    assert result.is_clean


def test_detects_a_value_corrupted_directly_in_the_database(db_session):
    save_candles(db_session, _candles("ETHUSDT", "1d", n=200))
    db_session.flush()
    calculate_indicators_for_symbol_timeframe(db_session, "ETHUSDT", "1d")
    db_session.flush()

    # Simula corrupción/un bug de otra capa escribiendo directo sobre una
    # fila ya persistida, sin pasar por save_indicators.
    row = (
        db_session.query(Indicador)
        .filter_by(symbol="ETHUSDT", timeframe="1d", indicator_name="RSI_14")
        .first()
    )
    assert row is not None
    row.value = row.value + Decimal("30")
    db_session.flush()

    result = verify_symbol_timeframe(db_session, "ETHUSDT", "1d")

    assert not result.is_clean
    assert any(m.kind == "valor_distinto" for m in result.mismatches)


def test_detects_a_row_deleted_directly_from_the_database(db_session):
    save_candles(db_session, _candles("SOLUSDT", "1d", n=200))
    db_session.flush()
    calculate_indicators_for_symbol_timeframe(db_session, "SOLUSDT", "1d")
    db_session.flush()

    row = (
        db_session.query(Indicador)
        .filter_by(symbol="SOLUSDT", timeframe="1d", indicator_name="ATR_14")
        .first()
    )
    assert row is not None
    db_session.delete(row)
    db_session.flush()

    result = verify_symbol_timeframe(db_session, "SOLUSDT", "1d")

    assert not result.is_clean
    assert any(m.kind == "faltante" for m in result.mismatches)


def test_no_candles_saved_is_reported_as_missing_data_not_a_failure(db_session):
    result = verify_symbol_timeframe(db_session, "AVAXUSDT", "1d")

    assert result.candles_used == 0
    assert result.is_clean
