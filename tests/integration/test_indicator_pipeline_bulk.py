"""Test de integración de `calculate_indicators_for_all` contra Postgres
real: múltiples símbolos/temporalidades, incluyendo una combinación sin
velas guardadas, y reintento sin duplicación.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.data.exchange_client import Candle
from app.data.indicator_pipeline import calculate_indicators_for_all
from app.repositories.candles import save_candles


def _candles(symbol: str, timeframe: str, n: int) -> list[Candle]:
    start = datetime(2024, 9, 1, tzinfo=timezone.utc)
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


def test_bulk_pipeline_across_symbols_and_skips_missing_combination(db_session):
    save_candles(db_session, _candles("BTCUSDT", "1d", n=150))
    save_candles(db_session, _candles("BTCUSDT", "1h", n=150))
    # ETHUSDT/1d se deja sin velas a propósito: debe declararse como
    # faltante, no romper la corrida de las demás combinaciones.
    db_session.flush()

    results = calculate_indicators_for_all(
        db_session, symbols=["BTCUSDT", "ETHUSDT"], timeframes=["1d", "1h"]
    )

    by_key = {(r.symbol, r.timeframe): r for r in results}
    assert by_key[("BTCUSDT", "1d")].calculated > 0
    assert by_key[("BTCUSDT", "1h")].calculated > 0
    assert by_key[("ETHUSDT", "1d")].candles_used == 0
    assert by_key[("ETHUSDT", "1h")].candles_used == 0


def test_bulk_pipeline_rerun_does_not_duplicate(db_session):
    save_candles(db_session, _candles("SOLUSDT", "1d", n=150))
    db_session.flush()

    first = calculate_indicators_for_all(db_session, symbols=["SOLUSDT"], timeframes=["1d"])
    second = calculate_indicators_for_all(db_session, symbols=["SOLUSDT"], timeframes=["1d"])

    assert first[0].inserted == first[0].calculated
    assert first[0].inserted > 0
    assert second[0].inserted == 0
