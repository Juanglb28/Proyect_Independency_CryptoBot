"""Persistencia del catálogo de eventos (sección 15.7 del plan).

A diferencia de `velas`/`indicadores` (hechos medidos, nunca se alteran
retroactivamente), `eventos` es contenido curado manualmente que el usuario
puede corregir con el tiempo — ver el docstring de `app/db/models/events.py`.
Por eso `save_events` hace upsert real (`ON CONFLICT ... DO UPDATE`) sobre
`external_id` en vez de `DO NOTHING`: volver a correr
`scripts/seed_events.py` tras editar el archivo de catálogo debe aplicar la
corrección, no ignorarla.

Esta capa SOLO persiste (sección 15.2 del plan: `repositories` no contiene
lógica de negocio). No decide qué eventos existen ni valida su contenido
más allá de los tipos de columna — eso es responsabilidad de quien arma el
archivo de catálogo y de `scripts/seed_events.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models.events import Evento


@dataclass(frozen=True)
class EventoInput:
    """Un evento del catálogo, tal como lo define el archivo de
    `data/events/*.json` — sin UUID todavía asignado (lo genera la base de
    datos en el primer insert; en un update conserva el que ya tenía)."""

    external_id: str
    event_date: date
    event_type: str
    description: str
    severity: str | None = None
    source: str | None = None
    scope: str | None = None


def save_events(session: Session, events: list[EventoInput]) -> int:
    """Inserta o actualiza `events` por `external_id`.

    Devuelve cuántos eventos son NUEVOS (insertados por primera vez); una
    actualización de un evento ya existente no cuenta, igual que
    `save_candles`/`save_indicators` no cuentan los conflictos como
    "insertados". El número de filas antes/después de un `DO UPDATE` no lo
    distingue `RETURNING` por sí solo, así que se compara contra los
    `external_id` ya presentes antes de ejecutar el upsert.
    """
    if not events:
        return 0

    incoming_ids = {e.external_id for e in events}
    existing_ids = set(
        session.execute(
            select(Evento.external_id).where(Evento.external_id.in_(incoming_ids))
        ).scalars()
    )

    statement = pg_insert(Evento.__table__).values(
        [
            {
                "external_id": e.external_id,
                "event_date": e.event_date,
                "event_type": e.event_type,
                "description": e.description,
                "severity": e.severity,
                "source": e.source,
                "scope": e.scope,
            }
            for e in events
        ]
    )
    statement = statement.on_conflict_do_update(
        constraint="uq_eventos_external_id",
        set_={
            "event_date": statement.excluded.event_date,
            "event_type": statement.excluded.event_type,
            "description": statement.excluded.description,
            "severity": statement.excluded.severity,
            "source": statement.excluded.source,
            "scope": statement.excluded.scope,
        },
    )
    session.execute(statement)
    session.flush()

    return len(incoming_ids - existing_ids)


def get_events(session: Session) -> list[Evento]:
    """Lee todo el catálogo, ordenado por fecha. Devuelve el modelo ORM
    directamente (a diferencia de `get_candles`/`get_indicators`): aquí no
    hay una versión "de dominio" separada todavía, y el volumen (40-60
    filas) no justifica introducir una."""
    statement = select(Evento).order_by(Evento.event_date.asc())
    return list(session.execute(statement).scalars().all())
