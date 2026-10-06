"""Persistencia de `trade_snapshots` (Paso 9; sección 8.5 del plan).

Esta capa SOLO persiste (sección 15.2): no calcula distancias, MAE/MFE ni
P&L no realizado — eso lo arma `app/results/snapshot_service.py` con las
fórmulas de `app/domain/rules/` y aquí llega como valores ya calculados.
Garantías de acceso a datos:

- `append_snapshot` es idempotente por `(position_id, snapshot_time,
  system_state)`: la restricción única de la base decide qué es duplicado
  (no un "buscar y luego insertar", que tendría una carrera). Reintentar
  devuelve `creado=False` y NO toca la fila existente.
- Solo-agregar: no hay funciones de actualización ni borrado, y un trigger
  de la base las rechazaría de todos modos.

La transacción la maneja quien llama.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models.snapshots import TradeSnapshot

_COLUMNAS = frozenset(c.name for c in TradeSnapshot.__table__.columns) - {"id", "created_at"}


def append_snapshot(session: Session, valores: Mapping[str, Any]) -> tuple[int | None, bool]:
    """Inserta un snapshot. Devuelve `(id, creado)`; `(None, False)` si ya
    existía uno con la misma clave de idempotencia."""
    desconocidas = set(valores) - _COLUMNAS
    if desconocidas:
        raise ValueError(f"columnas desconocidas para trade_snapshots: {sorted(desconocidas)}")
    stmt = (
        pg_insert(TradeSnapshot)
        .values(**valores)
        .on_conflict_do_nothing(index_elements=["position_id", "snapshot_time", "system_state"])
        .returning(TradeSnapshot.id)
    )
    nuevo = session.execute(stmt).scalar_one_or_none()
    return (nuevo, True) if nuevo is not None else (None, False)


def list_snapshots_for_position(session: Session, position_id: uuid.UUID) -> list[TradeSnapshot]:
    """La trayectoria de una posición, en orden cronológico (empates por
    `id`, es decir, por orden de escritura)."""
    return list(
        session.execute(
            select(TradeSnapshot)
            .where(TradeSnapshot.position_id == position_id)
            .order_by(TradeSnapshot.snapshot_time, TradeSnapshot.id)
        ).scalars()
    )


def get_price_extremes(
    session: Session, position_id: uuid.UUID, *, until: datetime | None = None
) -> tuple[Decimal, Decimal] | None:
    """`(mínimo, máximo)` de `market_price` de los snapshots de la posición
    (hasta `until` inclusive si se indica) o `None` si no tiene ninguno."""
    stmt = select(func.min(TradeSnapshot.market_price), func.max(TradeSnapshot.market_price)).where(
        TradeSnapshot.position_id == position_id
    )
    if until is not None:
        stmt = stmt.where(TradeSnapshot.snapshot_time <= until)
    minimo, maximo = session.execute(stmt).one()
    return None if minimo is None else (minimo, maximo)


def get_ultimo_snapshot(session: Session, position_id: uuid.UUID) -> TradeSnapshot | None:
    """El snapshot más reciente de una posición (por hora y luego id), o `None`."""
    return session.execute(
        select(TradeSnapshot)
        .where(TradeSnapshot.position_id == position_id)
        .order_by(TradeSnapshot.snapshot_time.desc(), TradeSnapshot.id.desc())
        .limit(1)
    ).scalar_one_or_none()
