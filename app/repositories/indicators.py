"""Persistencia de indicadores (sección 15.7 del plan: misma idempotencia
que `velas`, aquí por `symbol + timeframe + calculated_at + indicator_name`).

Esta capa SOLO persiste (sección 15.2: `repositories` no contiene lógica
de negocio). No calcula nada — eso vive en
`app/data/indicator_calculator.py`.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.data.indicator_calculator import IndicatorRow
from app.db.models.indicators import Indicador


def save_indicators(session: Session, rows: list[IndicatorRow]) -> int:
    """Inserta `rows`, ignorando las que ya existan.

    Mismo patrón que `repositories/candles.py::save_candles`: `ON CONFLICT
    DO NOTHING` sobre la restricción única, con `.returning()` explícito en
    vez de confiar en `result.rowcount` (que queda en -1 con psycopg3
    cuando SQLAlchemy agrega un RETURNING implícito para leer el `id`
    autogenerado — ver el docstring de `save_candles` para el detalle
    completo de por qué).
    """
    if not rows:
        return 0

    statement = (
        pg_insert(Indicador.__table__)
        .values(
            [
                {
                    "symbol": row.symbol,
                    "timeframe": row.timeframe,
                    "calculated_at": row.calculated_at,
                    "indicator_name": row.indicator_name,
                    "value": row.value,
                }
                for row in rows
            ]
        )
        .on_conflict_do_nothing(
            constraint="uq_indicadores_symbol_timeframe_calculated_at_indicator_name"
        )
        .returning(Indicador.__table__.c.id)
    )
    result = session.execute(statement)
    inserted_count = len(result.fetchall())
    session.flush()
    return inserted_count
