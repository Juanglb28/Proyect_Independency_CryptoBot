"""Tests unitarios de app.data.candle_ingestion.

Usan un `fetch_fn` falso en vez de red real, así que cubren solo la lógica
de paginación (cuándo pedir la siguiente página, cuándo parar) — no la
persistencia real (eso se prueba en
tests/integration/test_candle_ingestion.py, que sí necesita Postgres) ni
la descarga real (tests/integration/test_exchange_client.py).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.data.candle_ingestion import backfill_symbol_timeframe
from app.data.exchange_client import Candle


def _make_candle(symbol: str, timeframe: str, open_time: datetime) -> Candle:
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time=open_time,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("10"),
    )


class _FakePaginatedSource:
    """Simula Binance devolviendo `page_size` velas por llamada hasta
    agotar `total_candles`, empezando en `start`. Registra los `since` con
    los que se le llamó, para poder verificar que la paginación avanza
    correctamente y no vuelve a pedir una vela ya recibida."""

    def __init__(self, start: datetime, step: timedelta, total_candles: int, page_size: int):
        self.start = start
        self.step = step
        self.total_candles = total_candles
        self.page_size = page_size
        self.calls: list[datetime] = []

    def __call__(self, symbol, timeframe, limit, since, exchange=None):
        self.calls.append(since)
        offset = int((since - self.start) / self.step)
        if offset >= self.total_candles:
            return []
        remaining = self.total_candles - offset
        count = min(self.page_size, limit, remaining)
        return [
            _make_candle(symbol, timeframe, since + self.step * i) for i in range(count)
        ]


def _no_op_save(monkeypatch):
    """save_candles necesita una sesión real de DB; para probar solo la
    paginación se reemplaza por un contador que no toca la base de datos."""
    inserted_counts: list[int] = []

    def fake_save_candles(session, candles):
        inserted_counts.append(len(candles))
        return len(candles)

    monkeypatch.setattr("app.data.candle_ingestion.save_candles", fake_save_candles)
    return inserted_counts


def test_backfill_paginates_until_reaching_present(monkeypatch):
    _no_op_save(monkeypatch)
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    step = timedelta(days=1)
    source = _FakePaginatedSource(start=start, step=step, total_candles=25, page_size=10)

    result = backfill_symbol_timeframe(
        session=object(),  # no se usa de verdad porque save_candles está parcheado
        symbol="BTCUSDT",
        timeframe="1d",
        since=start,
        until=start + step * 25,
        fetch_fn=source,
        page_limit=10,  # igual al page_size del fake: una página parcial real
        # (Binance) significa "no hay más datos", así que el test debe pedir
        # exactamente lo que el fake puede entregar por página, igual que en
        # producción se pide `page_limit` y se recibe como máximo eso.
    )

    # 25 velas en páginas de 10: 10 + 10 + 5 = 3 páginas.
    assert result.pages == 3
    assert result.fetched == 25
    assert result.inserted == 25
    assert result.last_open_time == start + step * 24

    # Cada llamada debe pedir `since` estrictamente después de la última
    # vela de la página anterior — nunca repetir una vela ya recibida.
    assert source.calls == [start, start + step * 10, start + step * 20]


def test_backfill_stops_when_source_returns_empty(monkeypatch):
    """Un símbolo que no existía todavía en `since` (Binance devuelve
    lista vacía) no debe producir un bucle infinito ni un error."""
    _no_op_save(monkeypatch)
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)

    def always_empty(symbol, timeframe, limit, since, exchange=None):
        return []

    result = backfill_symbol_timeframe(
        session=object(),
        symbol="BTCUSDT",
        timeframe="1d",
        since=start,
        until=start + timedelta(days=365),
        fetch_fn=always_empty,
    )

    assert result.pages == 1
    assert result.fetched == 0
    assert result.inserted == 0
    assert result.last_open_time is None


def test_backfill_is_idempotent_on_rerun(monkeypatch):
    """Reintentar el mismo backfill (p. ej. tras un corte) debe reportar
    cero velas insertadas la segunda vez si ya estaban todas guardadas —
    simulado aquí porque `save_candles` real usa ON CONFLICT DO NOTHING
    (probado con Postgres real en el test de integración)."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    step = timedelta(days=1)
    already_saved: set[tuple[str, str, datetime]] = set()

    def fake_save_candles_with_conflict(session, candles):
        inserted = 0
        for candle in candles:
            key = (candle["symbol"], candle["timeframe"], candle["open_time"])
            if key not in already_saved:
                already_saved.add(key)
                inserted += 1
        return inserted

    monkeypatch.setattr(
        "app.data.candle_ingestion.save_candles", fake_save_candles_with_conflict
    )
    source = _FakePaginatedSource(start=start, step=step, total_candles=15, page_size=10)

    first = backfill_symbol_timeframe(
        session=object(), symbol="BTCUSDT", timeframe="1d",
        since=start, until=start + step * 15, fetch_fn=source, page_limit=10,
    )
    second = backfill_symbol_timeframe(
        session=object(), symbol="BTCUSDT", timeframe="1d",
        since=start, until=start + step * 15, fetch_fn=source, page_limit=10,
    )

    assert first.inserted == 15
    assert second.inserted == 0  # todo ya estaba guardado


def test_backfill_rejects_unsupported_timeframe():
    with pytest.raises(ValueError):
        backfill_symbol_timeframe(
            session=object(), symbol="BTCUSDT", timeframe="1m",
            since=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )


def test_backfill_rejects_naive_since():
    with pytest.raises(ValueError):
        backfill_symbol_timeframe(
            session=object(), symbol="BTCUSDT", timeframe="1d",
            since=datetime(2024, 1, 1),  # sin tzinfo a propósito
        )


def test_backfill_respects_page_safety_limit(monkeypatch):
    """Una ventana que necesitaría más páginas que la cota de seguridad
    (por ejemplo, por un `until` mal calculado en algún llamador futuro)
    debe cortar el bucle en vez de seguir pidiendo páginas indefinidamente."""
    _no_op_save(monkeypatch)
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    step = timedelta(days=1)
    # Con page_limit=10 y un día por vela, cubrir 100.000 días pediría del
    # orden de 10.000 páginas — muy por encima de la cota de seguridad.
    source = _FakePaginatedSource(start=start, step=step, total_candles=100_000, page_size=10)

    result = backfill_symbol_timeframe(
        session=object(), symbol="BTCUSDT", timeframe="1d",
        since=start, until=start + step * 100_000,
        fetch_fn=source, page_limit=10,
    )

    assert result.hit_page_safety_limit is True
