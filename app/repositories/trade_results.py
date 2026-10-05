"""Persistencia de `trade_results` (Paso 9; sección 8.4 del plan).

Esta capa SOLO persiste (sección 15.2): el resultado ya viene calculado por
`app/domain/rules/results.py`. Garantías de acceso a datos:

- `save_trade_result` es idempotente por `position_id` (sección 15.7: "una
  posición cerrada produce un único `trade_result` definitivo"): reintentar
  devuelve el resultado que ya existía y NO lo modifica.
- Solo-agregar: no hay funciones de actualización ni borrado, y un trigger de
  la base las rechazaría de todos modos.

La transacción la maneja quien llama.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models.positions import Posicion
from app.db.models.trade_results import TradeResultModel
from app.domain.enums import EstadoPosicion

_COLUMNAS = frozenset(c.name for c in TradeResultModel.__table__.columns) - {"id", "created_at"}


def save_trade_result(session: Session, valores: Mapping[str, Any]) -> tuple[uuid.UUID, bool]:
    """Inserta el resultado de una posición cerrada. Devuelve `(id, creado)`;
    `creado=False` si esa posición ya tenía resultado (reintento)."""
    desconocidas = set(valores) - _COLUMNAS
    if desconocidas:
        raise ValueError(f"columnas desconocidas para trade_results: {sorted(desconocidas)}")
    stmt = (
        pg_insert(TradeResultModel)
        .values(id=uuid.uuid4(), **valores)
        .on_conflict_do_nothing(index_elements=["position_id"])
        .returning(TradeResultModel.id)
    )
    nuevo = session.execute(stmt).scalar_one_or_none()
    if nuevo is not None:
        return nuevo, True
    existente = get_trade_result_by_position(session, valores["position_id"])
    if existente is None:  # pragma: no cover - solo si la fila se borró entre ambas sentencias
        raise RuntimeError("trade_result en conflicto pero no encontrado")
    return existente.id, False


def get_trade_result_by_position(session: Session, position_id: uuid.UUID) -> TradeResultModel | None:
    return session.execute(
        select(TradeResultModel).where(TradeResultModel.position_id == position_id)
    ).scalar_one_or_none()


def list_trade_results(
    session: Session,
    *,
    exit_desde: datetime | None = None,
    exit_hasta: datetime | None = None,
    symbol: str | None = None,
) -> list[TradeResultModel]:
    """Resultados cuya SALIDA cae en `[exit_desde, exit_hasta]` (ambos
    inclusivos si se indican), en orden cronológico de salida."""
    stmt = select(TradeResultModel).order_by(TradeResultModel.exit_time, TradeResultModel.id)
    if exit_desde is not None:
        stmt = stmt.where(TradeResultModel.exit_time >= exit_desde)
    if exit_hasta is not None:
        stmt = stmt.where(TradeResultModel.exit_time <= exit_hasta)
    if symbol is not None:
        stmt = stmt.where(TradeResultModel.symbol == symbol)
    return list(session.execute(stmt).scalars())


def list_closed_positions_without_result(session: Session) -> list[Posicion]:
    """Posiciones `CLOSED` que todavía no tienen `trade_result` (por si el
    cálculo falló justo al cerrar), de la más antigua a la más reciente."""
    return list(
        session.execute(
            select(Posicion)
            .outerjoin(TradeResultModel, TradeResultModel.position_id == Posicion.id)
            .where(Posicion.status == EstadoPosicion.CLOSED, TradeResultModel.id.is_(None))
            .order_by(Posicion.closed_at, Posicion.id)
        ).scalars()
    )
