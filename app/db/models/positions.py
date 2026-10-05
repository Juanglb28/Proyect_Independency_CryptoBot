"""Modelo ORM de `posiciones` (sección 6 y 8.2 del plan: "Estado actual,
precio de entrada, stop, objetivo"). Diseño aprobado por el usuario el
2026-10-04 — ver README, "Decisiones del Paso 8".

Es la ÚNICA tabla mutable de la capa de ejecución: refleja el estado vivo
de una posición (cantidad restante, TP parcial ya ejecutado, trailing). La
historia vive en otras tablas — `trade_snapshots` (trayectoria) y
`trade_results` (resultado definitivo), ambas del Paso 9 — y el stop/objetivo
INICIALES quedan inmutables aquí (`initial_stop_price`/`initial_target_price`)
porque son los que sustentan el R-multiple y la reproducibilidad.

Garantías que vive en la base de datos:

- Un símbolo no puede tener dos posiciones `OPEN` a la vez
  (`uq_posiciones_symbol_abierta`, índice único PARCIAL): la regla "nunca se
  abre un símbolo que ya tiene posición" no depende solo del código (P5).
- Una propuesta abre a lo sumo una posición (`proposal_id` único).
- Coherencia estado/cantidad (`ck_posiciones_estado_coherente`): `OPEN`
  siempre tiene cantidad restante > 0 y sin `closed_at`; `CLOSED` siempre
  tiene 0 restante y `closed_at`. Una posición "cerrada con cantidad" o
  "abierta sin cantidad" no puede existir.
- Si el trailing está activo, su precio máximo de referencia existe.

Cuántas posiciones hay abiertas, cuántas entradas hubo hoy y cuántas son de
mayor riesgo se CALCULAN consultando `posiciones`/`ordenes` — no hay
contadores aparte que puedan desincronizarse.

Dinero y precios en `NUMERIC`, fechas en `TIMESTAMPTZ` (UTC).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index, Numeric, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.enums import EstadoPosicion


class Posicion(Base):
    __tablename__ = "posiciones"
    __table_args__ = (
        CheckConstraint(
            "(status = 'OPEN' AND remaining_quantity > 0 AND closed_at IS NULL) "
            "OR (status = 'CLOSED' AND remaining_quantity = 0 AND closed_at IS NOT NULL)",
            name="estado_coherente",
        ),
        CheckConstraint("initial_quantity > 0", name="initial_quantity_positiva"),
        CheckConstraint(
            "remaining_quantity >= 0 AND remaining_quantity <= initial_quantity",
            name="remaining_en_rango",
        ),
        CheckConstraint("entry_price > 0", name="entry_price_positivo"),
        CheckConstraint(
            "initial_stop_price > 0 AND initial_target_price > 0",
            name="niveles_positivos",
        ),
        CheckConstraint(
            "NOT trailing_active OR trailing_peak_price IS NOT NULL",
            name="trailing_con_referencia",
        ),
        Index(
            "uq_posiciones_symbol_abierta",
            "symbol",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("propuestas.id"), nullable=False, unique=True
    )

    symbol: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[EstadoPosicion] = mapped_column(
        Enum(EstadoPosicion, name="estado_posicion", native_enum=False, length=8, validate_strings=True),
        nullable=False,
    )

    entry_price: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    initial_quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    remaining_quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)

    initial_stop_price: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    initial_target_price: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    """Niveles INICIALES calculados por `app/domain/rules/risk.py` al abrir
    (stop -8%, primer TP +12% desde la entrada). Inmutables: el trailing no
    los reescribe, se refleja en `trailing_peak_price`."""

    take_profit_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    """True cuando ya se ejecutó el cierre parcial del 50% en +12%."""
    trailing_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    trailing_peak_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    """Precio máximo observado desde que se activó el trailing, actualizado
    en cada chequeo y nunca a la baja (derivado del plan, ver README)."""

    reference_prices_at_entry: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    """Precios observados al abrir de BTC y de los pares del universo
    (`{"BTCUSDT": "67000.5", ...}`, precios como texto para no pasar por
    float), para el benchmark de la operación (Paso 9, decisión del
    2026-10-05). Se escribe UNA vez al crear la posición y nunca cambia. Solo
    trae los pares cuyo precio se pudo obtener: lo que falta no se inventa."""

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
