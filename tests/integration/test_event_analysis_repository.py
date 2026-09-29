"""Test de integración: `save_event_analysis`/`get_event_analysis` contra
Postgres real, mismo patrón de idempotencia que
tests/integration/test_indicators_repository.py."""

import uuid
from datetime import date
from decimal import Decimal

from app.data.event_analyzer import EventAnalysisRow
from app.repositories.event_analysis import get_event_analysis, save_event_analysis
from app.repositories.events import EventoInput, save_events


def _seed_one_event(session) -> uuid.UUID:
    save_events(
        session,
        [
            EventoInput(
                external_id="evento-test",
                event_date=date(2024, 1, 1),
                event_type="OTRO",
                description="x",
            )
        ],
    )
    from app.repositories.events import get_events

    return get_events(session)[0].id


def _row(evento_id: uuid.UUID, **overrides) -> EventAnalysisRow:
    defaults = dict(
        evento_id=evento_id,
        symbol="BTCUSDT",
        window_offset_days=1,
        reference_date=date(2024, 1, 1),
        reference_price=Decimal("100"),
        window_date=date(2024, 1, 2),
        window_price=Decimal("105"),
        price_change_pct=Decimal("5"),
    )
    defaults.update(overrides)
    return EventAnalysisRow(**defaults)


def test_save_event_analysis_inserts_new_rows(db_session):
    evento_id = _seed_one_event(db_session)

    inserted = save_event_analysis(db_session, [_row(evento_id)])

    assert inserted == 1
    rows = get_event_analysis(db_session, evento_id)
    assert len(rows) == 1
    assert rows[0].price_change_pct == Decimal("5")


def test_save_event_analysis_is_idempotent_on_rerun(db_session):
    evento_id = _seed_one_event(db_session)
    rows = [_row(evento_id)]

    first = save_event_analysis(db_session, rows)
    second = save_event_analysis(db_session, rows)

    assert first == 1
    assert second == 0


def test_save_event_analysis_distinguishes_by_symbol_and_window(db_session):
    evento_id = _seed_one_event(db_session)
    rows = [
        _row(evento_id, symbol="BTCUSDT", window_offset_days=1),
        _row(evento_id, symbol="BTCUSDT", window_offset_days=7),
        _row(evento_id, symbol="ETHUSDT", window_offset_days=1),
    ]

    inserted = save_event_analysis(db_session, rows)

    assert inserted == 3
    assert len(get_event_analysis(db_session, evento_id)) == 3


def test_save_event_analysis_with_empty_list_is_a_no_op(db_session):
    assert save_event_analysis(db_session, []) == 0
