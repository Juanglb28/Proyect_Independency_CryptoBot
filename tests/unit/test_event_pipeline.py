"""Tests unitarios de app.data.event_pipeline: `get_candles`/`get_events`/
`save_event_analysis` parcheados, así que cubren solo la orquestación (qué
símbolos se evalúan según `scope`, qué pasa sin velas, cuántas veces se hace
`commit()`) — el round-trip real se prueba en
tests/integration/test_event_pipeline_bulk.py."""

from dataclasses import dataclass
from datetime import date

from app.data.event_pipeline import calculate_event_analysis_for_all, calculate_event_analysis_for_event


@dataclass
class _FakeEvento:
    id: str
    external_id: str
    event_date: date
    scope: str | None


class _FakeSession:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def test_event_with_global_scope_evaluates_all_whitelist_symbols(monkeypatch):
    evento = _FakeEvento(id="e1", external_id="e1", event_date=date(2024, 1, 1), scope=None)

    seen_symbols = []

    def fake_get_candles(session, symbol, timeframe):
        seen_symbols.append(symbol)
        return []  # sin velas: no hace falta simular el cálculo real aquí

    monkeypatch.setattr("app.data.event_pipeline.get_candles", fake_get_candles)
    monkeypatch.setattr("app.data.event_pipeline.save_event_analysis", lambda session, rows: 0)

    from app.domain.symbols import ALLOWED_SYMBOLS

    result = calculate_event_analysis_for_event(session=object(), evento=evento)

    assert set(seen_symbols) == set(ALLOWED_SYMBOLS)
    assert result.symbols_evaluated == len(ALLOWED_SYMBOLS)
    assert set(result.symbols_sin_velas) == set(ALLOWED_SYMBOLS)


def test_event_with_specific_scope_evaluates_only_those_symbols(monkeypatch):
    evento = _FakeEvento(id="e1", external_id="e1", event_date=date(2024, 1, 1), scope="ETHUSDT")

    seen_symbols = []
    monkeypatch.setattr(
        "app.data.event_pipeline.get_candles",
        lambda session, symbol, timeframe: (seen_symbols.append(symbol) or []),
    )
    monkeypatch.setattr("app.data.event_pipeline.save_event_analysis", lambda session, rows: 0)

    result = calculate_event_analysis_for_event(session=object(), evento=evento)

    assert seen_symbols == ["ETHUSDT"]
    assert result.symbols_evaluated == 1


def test_calculate_for_all_commits_once_per_event(monkeypatch):
    eventos = [
        _FakeEvento(id="e1", external_id="e1", event_date=date(2024, 1, 1), scope="BTCUSDT"),
        _FakeEvento(id="e2", external_id="e2", event_date=date(2024, 2, 1), scope="ETHUSDT"),
    ]
    monkeypatch.setattr("app.data.event_pipeline.get_events", lambda session: eventos)
    monkeypatch.setattr("app.data.event_pipeline.get_candles", lambda session, symbol, timeframe: [])
    monkeypatch.setattr("app.data.event_pipeline.save_event_analysis", lambda session, rows: 0)

    session = _FakeSession()

    results = calculate_event_analysis_for_all(session)

    assert len(results) == 2
    assert session.commits == 2
