"""Modelo ORM de `ordenes` (sección 6 del plan: "Toda orden: intención,
estado, resultado, ID de idempotencia"). Diseño aprobado por el usuario el
2026-10-04 — ver README, "Decisiones del Paso 8".

Una fila por cada INTENCIÓN de mover dinero (entrada o cualquier salida),
no por cada intento técnico: un reintento reutiliza el mismo
`client_order_id` y choca con su restricción única en vez de duplicar
(P5 / sección 15.7 del plan).

Garantías que vive en la base de datos, no solo en el código:

- `client_order_id` único (idempotencia general).
- A lo sumo UNA orden `ENTRY` por propuesta (`uq_ordenes_entrada_por_propuesta`):
  aunque un reintento generara otro `client_order_id`, la misma propuesta
  nunca abre dos posiciones.
- Una orden `FILLED` siempre trae cantidad, precio de ejecución, comisión y
  fecha de ejecución (`ck_ordenes_filled_completa`): no puede existir una
  orden "ejecutada" sin los datos que `trade_results` necesita.

Una orden `PENDING_APPROVAL` todavía no tiene posición ni cantidad (la
cantidad sale de `monto_usd_solicitado / precio de ejecución`, y ese precio
solo existe al ejecutar): por eso `position_id`, `quantity` y los precios
son nulables.

Dinero y precios en `NUMERIC`, fechas en `TIMESTAMPTZ` (UTC) — nunca
`float` (sección 15.3 del plan).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.enums import EstadoOrden, IntencionOrden, LadoOrden


class Orden(Base):
    __tablename__ = "ordenes"
    __table_args__ = (
        CheckConstraint(
            "estado <> 'FILLED' OR (quantity IS NOT NULL AND execution_price IS NOT NULL "
            "AND fee IS NOT NULL AND filled_at IS NOT NULL)",
            name="filled_completa",
        ),
        CheckConstraint("quantity IS NULL OR quantity > 0", name="quantity_positiva"),
        CheckConstraint("execution_price IS NULL OR execution_price > 0", name="execution_price_positivo"),
        CheckConstraint("reference_price IS NULL OR reference_price > 0", name="reference_price_positivo"),
        CheckConstraint("fee IS NULL OR fee >= 0", name="fee_no_negativa"),
        CheckConstraint(
            "requested_usd IS NULL OR requested_usd > 0", name="requested_usd_positivo"
        ),
        # A lo sumo una entrada por propuesta, aunque cambie el client_order_id.
        Index(
            "uq_ordenes_entrada_por_propuesta",
            "proposal_id",
            unique=True,
            postgresql_where=text("intencion = 'ENTRY'"),
        ),
        Index("ix_ordenes_position_id", "position_id"),
        Index("ix_ordenes_estado_created_at", "estado", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_order_id: Mapped[str] = mapped_column(nullable=False, unique=True)
    """Clave de idempotencia: la decide quien crea la orden, de forma
    determinista a partir de la propuesta/posición y la intención, para que
    un reintento produzca el mismo valor."""

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("propuestas.id"), nullable=False
    )
    """Toda orden pertenece a una propuesta (las salidas heredan la de su
    posición) — es lo que permite reconstruir "qué decisión originó este
    dinero" sin joins ambiguos."""
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("posiciones.id"), nullable=True
    )
    """NULL mientras la entrada está `PENDING_APPROVAL` (o si fue rechazada o
    expiró): todavía no existe la posición."""

    symbol: Mapped[str] = mapped_column(nullable=False)
    side: Mapped[LadoOrden] = mapped_column(
        Enum(LadoOrden, name="lado_orden", native_enum=False, length=8, validate_strings=True), nullable=False
    )
    intencion: Mapped[IntencionOrden] = mapped_column(
        Enum(IntencionOrden, name="intencion_orden", native_enum=False, length=32, validate_strings=True),
        nullable=False,
    )
    estado: Mapped[EstadoOrden] = mapped_column(
        Enum(EstadoOrden, name="estado_orden", native_enum=False, length=24, validate_strings=True),
        nullable=False,
    )

    requested_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    """Monto en USD que pidió la entrada (100 en v1, decidido por
    `app/domain/rules/risk.py`). NULL en las salidas, que se definen por
    cantidad."""
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    reference_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    """Precio observado que disparó la orden (en una salida por stop, el
    precio visto en el chequeo, que puede ser peor que el nivel — decisión
    del 2026-10-04)."""
    execution_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    fee: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    """Comisión en USD (0,1% por lado en paper). El slippage no se guarda
    aquí: en v1 queda NULL en `trade_results` (decisión del 2026-10-04)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Solo para `PENDING_APPROVAL`: 00:00 UTC del día siguiente al del
    ciclo."""
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Cuándo se aprobó, rechazó o expiró la aprobación manual."""
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Motivo de un rechazo/expiración, en texto libre para auditoría."""
