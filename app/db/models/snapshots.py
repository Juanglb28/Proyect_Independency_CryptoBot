"""Modelo ORM de `trade_snapshots` (Paso 9; sección 8.5 del plan): la
TRAYECTORIA de una posición abierta. Un snapshot de rutina por cada chequeo
del monitor (`system_state = 'SCHEDULED'`) y uno inmediato por cada evento
relevante (ver `app.domain.enums.EstadoSnapshot`).

Reglas:

- **Solo-agregar**: un trigger rechaza `UPDATE` y `DELETE` ("no se debe
  sobrescribir un snapshot existente"). MAE/MFE de `trade_results` salen de
  estas filas, así que no pueden reescribirse.
- **Idempotencia**: `UNIQUE(position_id, snapshot_time, system_state)`
  (decidido en la sección 8.5). Un reintento del mismo segundo con el mismo
  estado colisiona y no duplica; un evento real en el mismo segundo que uno
  programado tiene otro `system_state` y no choca.
- Sin `trade_result_id`: el plan lo lista, pero rellenarlo al cerrar exigiría
  ACTUALIZAR un snapshot, y eso contradice la regla anterior. El vínculo es
  `position_id` (una posición tiene a lo sumo un `trade_result`).

Qué guarda cada columna (derivado de la sección 8.5; el usuario puede
corregirlo):

- `quantity`: cantidad de la posición en ese instante. Para `PARTIAL_TAKE_PROFIT`
  y `EXIT` es la cantidad ANTES de la venta (lo que estaba en juego), no la
  restante.
- `unrealized_pnl` / `unrealized_return_pct`: sobre esa cantidad, contra el
  precio de entrada y sin comisiones: `quantity × (precio - entrada)` y
  `(precio / entrada - 1) × 100`.
- `stop_price`: el stop INICIAL mientras no hay trailing; con el trailing
  activo, su nivel de salida (máximo × 94%). `target_price`: el objetivo
  inicial, o `NULL` una vez ejecutado el TP parcial (ya no hay objetivo).
- `distance_to_stop_pct` / `distance_to_target_pct`: `(precio - stop) /
  precio × 100` y `(objetivo - precio) / precio × 100`.
- `current_mae_pct` / `current_mfe_pct`: peor/mejor precio observado hasta
  ese snapshot (incluido) frente a la entrada.
- `market_volume`: `NULL` — la fuente de precios del monitor solo entrega
  precio. `atr`, `relative_volume`: último valor 1h de `indicadores` con
  `calculated_at <= snapshot_time` (sin look-ahead); `rsi_1h` / `rsi_4h` ídem
  en su temporalidad. Sin dato → `NULL`, nunca se rellena (P3).

Dinero y precios en `NUMERIC`, fechas en `TIMESTAMPTZ` (UTC).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Identity, Index, Numeric, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.enums import EstadoSnapshot


class TradeSnapshot(Base):
    __tablename__ = "trade_snapshots"
    __table_args__ = (
        CheckConstraint("market_price > 0", name="market_price_positivo"),
        CheckConstraint("quantity > 0", name="quantity_positiva"),
        CheckConstraint("position_value > 0", name="position_value_positivo"),
        CheckConstraint(
            "NOT trailing_active OR trailing_reference_price IS NOT NULL", name="trailing_con_referencia"
        ),
        Index("uq_trade_snapshots_evento", "position_id", "snapshot_time", "system_state", unique=True),
        Index("ix_trade_snapshots_position_time", "position_id", "snapshot_time"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    position_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("posiciones.id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(nullable=False)
    snapshot_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    market_price: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    market_volume: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    position_value: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    unrealized_return_pct: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    distance_to_stop_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    distance_to_target_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    trailing_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    trailing_reference_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    current_mae_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    current_mfe_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    atr: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    relative_volume: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    rsi_1h: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    rsi_4h: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    system_state: Mapped[EstadoSnapshot] = mapped_column(
        Enum(EstadoSnapshot, name="estado_snapshot", native_enum=False, length=30, validate_strings=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
