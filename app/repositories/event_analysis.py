"""Persistencia de `analisis_eventos` (sección 15.7 del plan): misma
idempotencia que `indicadores`, aquí por `evento_id + symbol +
window_offset_days`.

Nombrado `event_analysis.py` (no `analysis.py`, aunque la sección 15.1 del
plan solo lista un `repositories/analysis.py`): ese nombre ya lo ocupa
`ciclos_analisis` (una tabla distinta y sin relación, ya reservada por el
trabajo de Paso 2 antes de este Paso 5). Mismo criterio en
`app/db/models/event_analysis.py`.

Esta capa SOLO persiste (sección 15.2 del plan). No calcula nada — eso vive
en `app/data/event_analyzer.py`.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.data.event_analyzer import EventAnalysisRow
from app.db.models.event_analysis import AnalisisEvento

# Mismo límite y misma razón que app/repositories/indicators.py: Postgres no
# acepta más de 65.535 parámetros bind por consulta. El volumen aquí es
# mucho menor (≤60 eventos x 20 símbolos x 6 ventanas ≈ 7.200 filas en
# total, nunca en una sola llamada), pero se trocea igual por consistencia y
# para no repetir el mismo incidente si el catálogo o las ventanas crecen.
_MAX_ROWS_PER_INSERT = 2_000


def save_event_analysis(session: Session, rows: list[EventAnalysisRow]) -> int:
    """Inserta `rows`, ignorando las que ya existan (`ON CONFLICT DO
    NOTHING`, igual que `save_indicators`: a diferencia de `eventos`, esto
    es un resultado calculado a partir de velas, no contenido curado —
    corregirlo significa recalcularlo, no editarlo a mano)."""
    if not rows:
        return 0

    inserted_count = 0
    for start in range(0, len(rows), _MAX_ROWS_PER_INSERT):
        batch = rows[start : start + _MAX_ROWS_PER_INSERT]
        statement = (
            pg_insert(AnalisisEvento.__table__)
            .values(
                [
                    {
                        "evento_id": row.evento_id,
                        "symbol": row.symbol,
                        "window_offset_days": row.window_offset_days,
                        "reference_date": row.reference_date,
                        "reference_price": row.reference_price,
                        "window_date": row.window_date,
                        "window_price": row.window_price,
                        "price_change_pct": row.price_change_pct,
                    }
                    for row in batch
                ]
            )
            .on_conflict_do_nothing(constraint="uq_analisis_eventos_evento_symbol_window")
            .returning(AnalisisEvento.__table__.c.id)
        )
        result = session.execute(statement)
        inserted_count += len(result.fetchall())

    session.flush()
    return inserted_count


def get_event_analysis(session: Session, evento_id) -> list[AnalisisEvento]:
    """Lee las filas ya guardadas de un evento (todas las combinaciones de
    símbolo y ventana calculadas hasta ahora)."""
    statement = (
        select(AnalisisEvento)
        .where(AnalisisEvento.evento_id == evento_id)
        .order_by(AnalisisEvento.symbol.asc(), AnalisisEvento.window_offset_days.asc())
    )
    return list(session.execute(statement).scalars().all())
