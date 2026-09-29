"""Test de integración del pipeline completo de `analisis_eventos` contra
Postgres real: velas + evento reales, calcular, releer, y reintento sin
duplicación. Mismo patrón que
tests/integration/test_indicator_pipeline_bulk.py."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.data.event_pipeline import calculate_event_analysis_for_all, calculate_event_analysis_for_event
from app.data.exchange_client import Candle
from app.repositories.candles import save_candles
from app.repositories.event_analysis import get_event_analysis
from app.repositories.events import EventoInput, get_events, save_events


def _daily_candles(symbol: str, start: datetime, n: int) -> list[Candle]:
    return [
        Candle(
            symbol=symbol,
            timeframe="1d",
            open_time=start + timedelta(days=i),
            open=Decimal(100 + i),
            high=Decimal(100 + i + 1),
            low=Decimal(100 + i - 1),
            close=Decimal(100 + i),
            volume=Decimal(1000),
        )
        for i in range(n)
    ]


def test_full_pipeline_round_trip_for_a_scoped_event(db_session):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    save_candles(db_session, _daily_candles("BTCUSDT", start, n=60))
    save_events(
        db_session,
        [
            EventoInput(
                external_id="evento-btc",
                event_date=(start + timedelta(days=20)).date(),
                event_type="OTRO",
                description="x",
                scope="BTCUSDT",  # solo BTCUSDT, no las otras 19 de la whitelist
            )
        ],
    )
    db_session.flush()
    evento = get_events(db_session)[0]

    result = calculate_event_analysis_for_event(db_session, evento)
    db_session.flush()

    assert result.symbols_evaluated == 1  # scope acotó a un solo símbolo
    assert result.symbols_sin_velas == ()
    assert result.inserted == result.calculated
    assert result.inserted > 0

    rows = get_event_analysis(db_session, evento.id)
    assert len(rows) == result.inserted
    assert all(r.symbol == "BTCUSDT" for r in rows)


def test_rerun_does_not_duplicate(db_session):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    save_candles(db_session, _daily_candles("SOLUSDT", start, n=60))
    save_events(
        db_session,
        [
            EventoInput(
                external_id="evento-sol",
                event_date=(start + timedelta(days=20)).date(),
                event_type="OTRO",
                description="x",
                scope="SOLUSDT",
            )
        ],
    )
    db_session.flush()
    evento = get_events(db_session)[0]

    first = calculate_event_analysis_for_event(db_session, evento)
    second = calculate_event_analysis_for_event(db_session, evento)

    assert first.inserted > 0
    assert second.inserted == 0


def test_global_event_without_candles_reports_missing_not_an_error(db_session):
    save_events(
        db_session,
        [
            EventoInput(
                external_id="evento-global-sin-velas",
                event_date=datetime(2024, 1, 1, tzinfo=timezone.utc).date(),
                event_type="OTRO",
                description="x",
                # scope=None -> GLOBAL: evalúa los 20 símbolos, ninguno con velas
            )
        ],
    )
    db_session.flush()

    results = calculate_event_analysis_for_all(db_session)

    assert len(results) == 1
    assert results[0].calculated == 0
    assert results[0].inserted == 0
    assert len(results[0].symbols_sin_velas) == results[0].symbols_evaluated
