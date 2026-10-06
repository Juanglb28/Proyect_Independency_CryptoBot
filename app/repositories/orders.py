"""Persistencia de `ordenes` (Paso 8; sección 15.7 del plan: idempotencia).

Esta capa SOLO persiste (sección 15.2). No decide montos, cantidades,
comisiones ni si una entrada necesita aprobación — eso vive en
`app/domain/rules/` y en los servicios de `app/execution/`. Aquí solo se
garantiza lo que debe garantizar el acceso a datos:

- `create_orden` es idempotente por `client_order_id`: reintentar con el
  mismo identificador devuelve la orden que ya existía, nunca crea otra
  (P5). La restricción única de la base de datos decide qué es duplicado,
  no un "buscar primero e insertar después" (que tendría una carrera).
  Un choque con OTRA restricción (por ejemplo una segunda entrada para la
  misma propuesta con distinto `client_order_id`) NO se silencia: lanza
  `IntegrityError`, porque ahí no hay un reintento sino un error real.
- `resolve_pending_order` solo transiciona una orden que SIGA en
  `PENDING_APPROVAL` (UPDATE condicionado al estado): dos resoluciones
  concurrentes o un reintento no pueden aprobar y rechazar la misma orden;
  la segunda devuelve `False`.
- Los conteos y listados reciben los estados/ventanas ya decididos por
  quien llama; esta capa no sabe qué cuenta como "cupo ocupado" (eso lo
  define el docstring de `SituacionDeCupos` en `risk.py`).

La transacción la maneja quien llama (igual que el resto de repositorios).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models.orders import Orden
from app.domain.enums import EstadoOrden, IntencionOrden, LadoOrden


@dataclass(frozen=True)
class OrdenRow:
    """Una orden lista para persistir. Los campos opcionales siguen las
    mismas reglas que las restricciones de la tabla (p. ej. una orden
    `FILLED` debe traer cantidad, precio de ejecución, comisión y
    `filled_at`, o la base de datos la rechaza)."""

    client_order_id: str
    proposal_id: uuid.UUID
    symbol: str
    side: LadoOrden
    intencion: IntencionOrden
    estado: EstadoOrden
    position_id: uuid.UUID | None = None
    requested_usd: Decimal | None = None
    quantity: Decimal | None = None
    reference_price: Decimal | None = None
    execution_price: Decimal | None = None
    fee: Decimal | None = None
    expires_at: datetime | None = None
    resolved_at: datetime | None = None
    filled_at: datetime | None = None
    detail: str | None = None
    created_at: datetime | None = None
    """Momento LÓGICO de la orden (el `now` del servicio). Si es `None`, la
    base de datos pone su reloj. Los servicios lo pasan siempre: así "entradas
    de hoy" se cuenta con el mismo reloj con que se decide (reproducible y sin
    depender de la hora real de la máquina)."""


def create_orden(session: Session, row: OrdenRow) -> tuple[uuid.UUID, bool]:
    """Inserta la orden si su `client_order_id` no existe. Devuelve
    `(id, creada)`: `creada=False` significa que ya existía una orden con ese
    `client_order_id` (reintento) y se devuelve ESA, sin tocarla."""
    values = {
        "id": uuid.uuid4(),
        "client_order_id": row.client_order_id,
        "proposal_id": row.proposal_id,
        "position_id": row.position_id,
        "symbol": row.symbol,
        "side": row.side,
        "intencion": row.intencion,
        "estado": row.estado,
        "requested_usd": row.requested_usd,
        "quantity": row.quantity,
        "reference_price": row.reference_price,
        "execution_price": row.execution_price,
        "fee": row.fee,
        "expires_at": row.expires_at,
        "resolved_at": row.resolved_at,
        "filled_at": row.filled_at,
        "detail": row.detail,
    }
    if row.created_at is not None:
        values["created_at"] = row.created_at
    stmt = (
        pg_insert(Orden)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["client_order_id"])
        .returning(Orden.id)
    )
    nuevo_id = session.execute(stmt).scalar_one_or_none()
    if nuevo_id is not None:
        return nuevo_id, True

    existente = get_orden_by_client_order_id(session, row.client_order_id)
    if existente is None:  # pragma: no cover - solo si la fila se borró entre ambas sentencias
        raise RuntimeError(f"client_order_id {row.client_order_id!r} en conflicto pero no encontrado")
    return existente.id, False


def get_orden(session: Session, orden_id: uuid.UUID) -> Orden | None:
    return session.get(Orden, orden_id)


def get_orden_by_client_order_id(session: Session, client_order_id: str) -> Orden | None:
    return session.execute(
        select(Orden).where(Orden.client_order_id == client_order_id)
    ).scalar_one_or_none()


def resolve_pending_order(
    session: Session,
    orden_id: uuid.UUID,
    *,
    nuevo_estado: EstadoOrden,
    resolved_at: datetime,
    detail: str | None = None,
    position_id: uuid.UUID | None = None,
    quantity: Decimal | None = None,
    execution_price: Decimal | None = None,
    fee: Decimal | None = None,
    filled_at: datetime | None = None,
) -> bool:
    """Pasa una orden `PENDING_APPROVAL` a `FILLED`, `REJECTED` o `EXPIRED`.

    El UPDATE está condicionado a `estado = 'PENDING_APPROVAL'`: devuelve
    `True` si hizo la transición y `False` si la orden ya estaba resuelta (o
    no existe) — en ese caso no modifica nada. Para `FILLED` quien llama
    debe pasar cantidad, precio, comisión y `filled_at`; si faltan, la
    restricción `ck_ordenes_filled_completa` de la base de datos rechaza el
    cambio (`IntegrityError`)."""
    if nuevo_estado == EstadoOrden.PENDING_APPROVAL:
        raise ValueError("resolve_pending_order no puede dejar la orden en PENDING_APPROVAL")

    result = session.execute(
        update(Orden)
        .where(Orden.id == orden_id, Orden.estado == EstadoOrden.PENDING_APPROVAL)
        .values(
            estado=nuevo_estado,
            resolved_at=resolved_at,
            detail=detail,
            position_id=position_id,
            quantity=quantity,
            execution_price=execution_price,
            fee=fee,
            filled_at=filled_at,
        )
    )
    return result.rowcount == 1


def list_orders(
    session: Session,
    *,
    estados: Collection[EstadoOrden],
    intencion: IntencionOrden | None = None,
) -> list[Orden]:
    """Órdenes en cualquiera de `estados` (opcionalmente de una intención),
    de la más antigua a la más reciente, con desempate por `id` para que el
    orden sea reproducible."""
    stmt = select(Orden).where(Orden.estado.in_(list(estados)))
    if intencion is not None:
        stmt = stmt.where(Orden.intencion == intencion)
    return list(session.execute(stmt.order_by(Orden.created_at, Orden.id)).scalars())


def list_pending_orders_expired_at(session: Session, now: datetime) -> list[Orden]:
    """Órdenes `PENDING_APPROVAL` cuyo `expires_at` ya llegó (`<= now`).
    Solo las lista; marcarlas `EXPIRED` es una decisión del servicio."""
    stmt = (
        select(Orden)
        .where(Orden.estado == EstadoOrden.PENDING_APPROVAL, Orden.expires_at <= now)
        .order_by(Orden.created_at, Orden.id)
    )
    return list(session.execute(stmt).scalars())


def count_entry_orders(
    session: Session,
    *,
    estados: Collection[EstadoOrden],
    created_from: datetime,
    created_before: datetime,
) -> int:
    """Cuántas órdenes `ENTRY` en `estados` se crearon en
    `[created_from, created_before)`. Quien llama define qué estados y qué
    ventana UTC cuentan como "entradas del día"."""
    stmt = select(func.count()).select_from(Orden).where(
        Orden.intencion == IntencionOrden.ENTRY,
        Orden.estado.in_(list(estados)),
        Orden.created_at >= created_from,
        Orden.created_at < created_before,
    )
    return int(session.execute(stmt).scalar_one())


def list_orders_for_position(session: Session, position_id: uuid.UUID) -> list[Orden]:
    """Todas las órdenes ligadas a una posición, en orden cronológico."""
    return list(
        session.execute(
            select(Orden).where(Orden.position_id == position_id).order_by(Orden.created_at, Orden.id)
        ).scalars()
    )


def list_orders_for_proposal(session: Session, proposal_id: uuid.UUID) -> list[Orden]:
    """Órdenes ligadas a una propuesta (a lo sumo una de entrada), en orden
    cronológico."""
    return list(
        session.execute(
            select(Orden).where(Orden.proposal_id == proposal_id).order_by(Orden.created_at, Orden.id)
        ).scalars()
    )
