"""Tests unitarios de app.data.indicator_pipeline.

Usan `get_candles`/`save_indicators` parcheados en vez de Postgres real, así
que cubren solo la lógica de orquestación (qué combinaciones se corren, qué
pasa cuando una no tiene velas) — no la persistencia real (eso se prueba en
tests/integration/test_indicator_pipeline_bulk.py)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.data.exchange_client import Candle
from app.data.indicator_pipeline import (
    calculate_indicators_for_all,
    calculate_indicators_for_symbol_timeframe,
)


def _candles(symbol: str, timeframe: str, n: int) -> list[Candle]:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        Candle(
            symbol=symbol,
            timeframe=timeframe,
            open_time=start + timedelta(days=i),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
        )
        for i in range(n)
    ]


def _patch_repositories(monkeypatch, candles_by_key: dict[tuple[str, str], list[Candle]]):
    saved: list[int] = []

    def fake_get_candles(session, symbol, timeframe):
        return candles_by_key.get((symbol, timeframe), [])

    def fake_save_indicators(session, rows):
        saved.append(len(rows))
        return len(rows)

    monkeypatch.setattr("app.data.indicator_pipeline.get_candles", fake_get_candles)
    monkeypatch.setattr("app.data.indicator_pipeline.save_indicators", fake_save_indicators)
    return saved


def test_calculate_for_symbol_timeframe_with_no_candles_is_declared_missing_not_an_error(monkeypatch):
    _patch_repositories(monkeypatch, {})

    result = calculate_indicators_for_symbol_timeframe(session=object(), symbol="BTCUSDT", timeframe="1d")

    assert result.candles_used == 0
    assert result.calculated == 0
    assert result.inserted == 0


def test_calculate_for_symbol_timeframe_computes_and_saves(monkeypatch):
    candles = _candles("BTCUSDT", "1d", n=200)
    _patch_repositories(monkeypatch, {("BTCUSDT", "1d"): candles})

    result = calculate_indicators_for_symbol_timeframe(session=object(), symbol="BTCUSDT", timeframe="1d")

    assert result.candles_used == 200
    assert result.calculated > 0
    assert result.inserted == result.calculated


class _FakeSession:
    """Cuenta cuántas veces se llamó a `commit()` — debe ser una vez por
    combinación símbolo/temporalidad, no una sola vez al final (P5 del plan:
    ver docstring de `calculate_indicators_for_all`)."""

    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def test_calculate_for_all_commits_once_per_combination(monkeypatch):
    candles_by_key = {
        ("BTCUSDT", "1d"): _candles("BTCUSDT", "1d", n=200),
        ("BTCUSDT", "1h"): _candles("BTCUSDT", "1h", n=200),
        ("ETHUSDT", "1d"): [],  # todavía sin backfill — debe saltarse, no fallar
    }
    _patch_repositories(monkeypatch, candles_by_key)
    session = _FakeSession()

    results = calculate_indicators_for_all(
        session, symbols=["BTCUSDT", "ETHUSDT"], timeframes=["1d", "1h"]
    )

    # 2 símbolos x 2 temporalidades = 4 combinaciones, aunque una no tenga velas.
    assert len(results) == 4
    assert session.commits == 4

    by_key = {(r.symbol, r.timeframe): r for r in results}
    assert by_key[("ETHUSDT", "1d")].candles_used == 0
    assert by_key[("BTCUSDT", "1d")].calculated > 0
    assert by_key[("BTCUSDT", "1h")].calculated > 0
    # ETHUSDT/1h no estaba en candles_by_key en absoluto: debe tratarse igual
    # que una lista vacía (dato faltante), no lanzar KeyError.
    assert by_key[("ETHUSDT", "1h")].candles_used == 0
