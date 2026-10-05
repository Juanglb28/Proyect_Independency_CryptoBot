"""Persistencia de `posiciones` (Paso 8; P5 del plan).

Esta capa SOLO persiste (sección 15.2). No calcula stops, objetivos,
cantidades ni decide cuándo cerrar — eso viene de `app/domain/rules/risk.py`
y lo orquesta `app/execution/`. Garantías de acceso a datos:

- `create_posicion` es idempotente por `proposal_id`: una propuesta abre a
  lo sumo una posición; reintentar devuelve la ya creada (P5). Un choque con
  el índice de "una posición abierta por símbolo" NO se silencia
  (`IntegrityError`): no es un reintento, es un intento real de duplicar.
- `update_open_position` solo modifica posiciones que SIGAN `OPEN`
  (UPDATE condicionado): una posición cerrada es historia y no se reescribe;
  devuelve `False` si no hizo nada. La coherencia estado/cantidad la exige
  la base de datos (`ck_posiciones_estado_coherente`).
- Nunca se modifican `entry_price`, `initial_quantity`, `initial_stop_price`
  ni `initial_target_price` después de crear la fila (el snapshot de la
  decisión no se reescribe — sección 14 del plan): `update_open_position`
  ni siquiera los acepta como parámetro.

La transacción la maneja quien llama.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models.positions import Posicion
from app.domain.enums import EstadoPosicion


@dataclass(frozen=True)
class PosicionRow:
    """Una posición recién abierta, lista para persistir."""

    proposal_id: uuid.UUID
    symbol: str
    entry_price: Decimal
    initial_quantity: Decimal
    initial_stop_price: Decimal
    initial_target_price: Decimal
    opened_at: datetime


def create_posicion(session: Session, row: PosicionRow) -> tuple[uuid.UUID, bool]:
    """Abre la posición de una propuesta (`OPEN`, con toda la cantidad
    restante). Devuelve `(id, creada)`; `creada=False` si esa propuesta ya
    tenía posición (reintento), y entonces devuelve la existente intacta."""
    stmt = (
        pg_insert(Posicion)
        .values(
            id=uuid.uuid4(),
            proposal_id=row.proposal_id,
            symbol=row.symbol,
            status=EstadoPosicion.OPEN,
            entry_price=row.entry_price,
            initial_quantity=row.initial_quantity,
            remaining_quantity=row.initial_quantity,
            initial_stop_price=row.initial_stop_price,
            initial_target_price=row.initial_target_price,
            opened_at=row.opened_at,
        )
        .on_conflict_do_nothing(index_elements=["proposal_id"])
        .returning(Posicion.id)
    )
    nuevo_id = session.execute(stmt).scalar_one_or_none()
    if nuevo_id is not None:
        return nuevo_id, True

    existente = get_posicion_by_proposal(session, row.proposal_id)
    if existente is None:  # pragma: no cover - solo si la fila se borró entre ambas sentencias
        raise RuntimeError(f"proposal_id {row.proposal_id} en conflicto pero sin posición")
    return existente.id, False


def get_posicion(session: Session, position_id: uuid.UUID) -> Posicion | None:
    return session.get(Posicion, position_id)


def get_posicion_by_proposal(session: Session, proposal_id: uuid.UUID) -> Posicion | None:
    return session.execute(
        select(Posicion).where(Posicion.proposal_id == proposal_id)
    ).scalar_one_or_none()


def get_open_posicion_by_symbol(session: Session, symbol: str) -> Posicion | None:
    return session.execute(
        select(Posicion).where(Posicion.symbol == symbol, Posicion.status == EstadoPosicion.OPEN)
    ).scalar_one_or_none()


def list_open_posiciones(session: Session) -> list[Posicion]:
    """Posiciones abiertas, de la más antigua a la más reciente (desempate
    por `id`: orden reproducible para el monitor)."""
    return list(
        session.execute(
            select(Posicion)
            .where(Posicion.status == EstadoPosicion.OPEN)
            .order_by(Posicion.opened_at, Posicion.id)
        ).scalars()
    )


def list_posiciones(session: Session) -> list[Posicion]:
    """Todas las posiciones (abiertas y cerradas), de la más antigua a la más
    reciente, con desempate por `id`. Para la reconciliación."""
    return list(session.execute(select(Posicion).order_by(Posicion.opened_at, Posicion.id)).scalars())


def count_open_posiciones(session: Session) -> int:
    return int(
        session.execute(
            select(func.count()).select_from(Posicion).where(Posicion.status == EstadoPosicion.OPEN)
        ).scalar_one()
    )


def update_open_position(
    session: Session,
    position_id: uuid.UUID,
    *,
    status: EstadoPosicion,
    remaining_quantity: Decimal,
    take_profit_hit: bool,
    trailing_active: bool,
    trailing_peak_price: Decimal | None,
    closed_at: datetime | None,
) -> bool:
    """Aplica a una posición `OPEN` el estado que calculó el dominio tras un
    chequeo del monitor (cierre parcial, trailing, cierre total). Devuelve
    `True` si actualizó y `False` si la posición ya no estaba abierta (o no
    existe), sin modificar nada. Los campos de la decisión original no son
    parámetros: no se pueden alterar."""
    result = session.execute(
        update(Posicion)
        .where(Posicion.id == position_id, Posicion.status == EstadoPosicion.OPEN)
        .values(
            status=status,
            remaining_quantity=remaining_quantity,
            take_profit_hit=take_profit_hit,
            trailing_active=trailing_active,
            trailing_peak_price=trailing_peak_price,
            closed_at=closed_at,
        )
    )
    return result.rowcount == 1
