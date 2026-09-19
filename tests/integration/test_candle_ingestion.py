"""Test de integración: backfill completo (paginación + persistencia real)
contra Postgres, con una fuente de velas falsa en vez de Binance real.

Cubre exactamente el caso que pide la sección 23 del plan: "reinicio con
posición abierta" / "retry sin duplicación" — aquí, reintentar el mismo
backfill tras una interrupción no debe duplicar ninguna vela.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.data.candle_ingestion import backfill_symbol_timeframe
from app.data.exchange_client import Candle
from app.db.models.candles import Vela


def _fake_source(start: datetime, step: timedelta, total: int, page_size: int):
    def fetch(symbol, timeframe, limit, since, exchange=None):
        offset = int((since - start) / step)
        if offset >= total:
            return []
        count = min(page_size, limit, total - offset)
        return [
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=since + step * i,
                open=Decimal("100"),
                high=Decimal("110"),
                low=Decimal("90"),
                close=Decimal("105"),
                volume=Decimal("10"),
            )
            for i in range(count)
        ]

    return fetch


def test_backfill_persists_all_candles_across_pages(db_session):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    step = timedelta(days=1)
    source = _fake_source(start, step, total=15, page_size=10)

    result = backfill_symbol_timeframe(
        db_session, "BTCUSDT", "1d", since=start,
        until=start + step * 15, fetch_fn=source, page_limit=10,
    )
    db_session.commit()

    assert result.inserted == 15
    stored = db_session.query(Vela).filter_by(symbol="BTCUSDT", timeframe="1d").count()
    assert stored == 15


def test_backfill_rerun_after_interruption_does_not_duplicate(db_session):
    """Simula un corte a mitad del backfill: la primera corrida cubre solo
    una parte de la ventana, la segunda vuelve a correr desde el mismo
    `since` original. Ninguna vela debe quedar duplicada."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    step = timedelta(days=1)
    source = _fake_source(start, step, total=20, page_size=10)

    # "Corte": solo alcanza a cubrir hasta la mitad de la ventana.
    partial = backfill_symbol_timeframe(
        db_session, "ETHUSDT", "1d", since=start,
        until=start + step * 10, fetch_fn=source, page_limit=10,
    )
    db_session.commit()
    assert partial.inserted == 10

    # Reintento: mismo `since`, ventana completa. No debe duplicar las 10
    # ya guardadas, solo agregar las 10 que faltaban.
    full = backfill_symbol_timeframe(
        db_session, "ETHUSDT", "1d", since=start,
        until=start + step * 20, fetch_fn=source, page_limit=10,
    )
    db_session.commit()
    assert full.inserted == 10

    stored = db_session.query(Vela).filter_by(symbol="ETHUSDT", timeframe="1d").count()
    assert stored == 20
