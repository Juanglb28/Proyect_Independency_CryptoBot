"""Test de integración de extremo a extremo: velas ya guardadas en Postgres
-> `get_candles` -> `calculate_indicators` -> `save_indicators`.

Cubre lo que hará `app/main.py::run_calculate_indicators` en producción,
sin red hacia Binance (usa velas insertadas directamente en la base de
datos de prueba).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.data.exchange_client import Candle
from app.data.indicator_calculator import INDICATOR_NAME_RSI, calculate_indicators
from app.repositories.candles import get_candles, save_candles
from app.repositories.indicators import save_indicators


def _candles(symbol: str, timeframe: str, n: int, step: timedelta) -> list[Candle]:
    start = datetime(2024, 6, 1, tzinfo=timezone.utc)
    price = Decimal("100")
    out: list[Candle] = []
    for i in range(n):
        price = price + Decimal(i % 5) - Decimal(2)
        out.append(
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=start + step * i,
                open=price,
                high=price + Decimal(1),
                low=price - Decimal(1),
                close=price,
                volume=Decimal(100 + i),
            )
        )
    return out


def test_full_pipeline_reads_calculates_and_persists(db_session):
    candles = _candles("BTCUSDT", "1d", n=100, step=timedelta(days=1))
    save_candles(db_session, candles)
    db_session.flush()

    fetched = get_candles(db_session, "BTCUSDT", "1d")
    assert len(fetched) == 100
    # get_candles debe devolver en orden ascendente por open_time.
    assert fetched == sorted(fetched, key=lambda c: c["open_time"])

    rows = calculate_indicators("BTCUSDT", "1d", fetched)
    inserted = save_indicators(db_session, rows)

    assert inserted == len(rows)
    assert any(row.indicator_name == INDICATOR_NAME_RSI for row in rows)


def test_full_pipeline_rerun_does_not_duplicate(db_session):
    """Recalcular sobre las mismas velas (p. ej. correr el comando dos
    veces) no debe duplicar ningún valor de indicador — sección 15.7 del
    plan, mismo principio que el backfill de velas."""
    candles = _candles("ETHUSDT", "1d", n=100, step=timedelta(days=1))
    save_candles(db_session, candles)
    db_session.flush()

    fetched = get_candles(db_session, "ETHUSDT", "1d")
    rows = calculate_indicators("ETHUSDT", "1d", fetched)

    first = save_indicators(db_session, rows)
    second = save_indicators(db_session, rows)

    assert first == len(rows)
    assert second == 0
