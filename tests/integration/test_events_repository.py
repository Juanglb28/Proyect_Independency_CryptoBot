"""Test de integración: `save_events` contra Postgres real, en particular
el upsert por `external_id` (a diferencia de `save_candles`/`save_indicators`,
que nunca actualizan una fila existente — ver docstring de
app/repositories/events.py)."""

from datetime import date

from app.repositories.events import EventoInput, get_events, save_events


def _event(**overrides) -> EventoInput:
    defaults = dict(
        external_id="test-event-1",
        event_date=date(2024, 1, 1),
        event_type="OTRO",
        description="Evento de prueba",
        severity="media",
        source="https://example.com",
        scope="BTCUSDT",
    )
    defaults.update(overrides)
    return EventoInput(**defaults)


def test_save_events_inserts_new_events(db_session):
    inserted = save_events(db_session, [_event()])

    assert inserted == 1
    events = get_events(db_session)
    assert len(events) == 1
    assert events[0].external_id == "test-event-1"
    assert events[0].description == "Evento de prueba"


def test_save_events_rerun_with_same_content_does_not_count_as_new(db_session):
    save_events(db_session, [_event()])

    second = save_events(db_session, [_event()])

    assert second == 0
    assert len(get_events(db_session)) == 1


def test_save_events_upserts_corrections_by_external_id(db_session):
    """A diferencia de velas/indicadores, un evento se puede corregir: la
    misma external_id con description/severity distintos debe actualizar la
    fila existente, no crear una segunda."""
    save_events(db_session, [_event(description="Descripción con un typo")])

    corrected = _event(description="Descripción corregida", severity="alta")
    inserted_again = save_events(db_session, [corrected])

    events = get_events(db_session)
    assert inserted_again == 0  # no es un evento nuevo, es una corrección
    assert len(events) == 1
    assert events[0].description == "Descripción corregida"
    assert events[0].severity == "alta"


def test_save_events_with_empty_list_is_a_no_op(db_session):
    assert save_events(db_session, []) == 0


def test_get_events_orders_by_date(db_session):
    save_events(
        db_session,
        [
            _event(external_id="later", event_date=date(2024, 6, 1)),
            _event(external_id="earlier", event_date=date(2024, 1, 1)),
        ],
    )

    events = get_events(db_session)

    assert [e.external_id for e in events] == ["earlier", "later"]
