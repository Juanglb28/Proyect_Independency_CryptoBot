"""Modelo ORM de `trade_results` (Paso 9; sección 8.4 del plan): el resultado
DEFINITIVO de una operación cerrada y fuente histórica primaria de toda
métrica. No depende de la interfaz ni de agregados precalculados.

Garantías que vive en la base de datos:

- **Un solo resultado por posición** (`position_id` único, sección 15.7): un
  reintento del servicio no crea un segundo resultado.
- **Solo-agregar**: un trigger rechaza `UPDATE` y `DELETE`. Si algún día hay
  un error de cálculo, el plan pide "una nueva versión/corrección
  auditable", no reescribir (sección 15.3). TODO — DECISIÓN PENDIENTE: ese
  mecanismo de corrección no existe todavía porque ninguna regla de v1 lo
  necesita; hasta definirlo, una corrección exige una decisión humana.
- Coherencia: `exit_time >= entry_time`, cantidades y precios positivos,
  `side = 'LONG'` (v1 es solo spot largo, sección 3), `exit_reason` nunca
  `ENTRY` y `exit_quantity = entry_quantity` (solo se crea con la posición
  cerrada del todo; el plan permite `exit_quantity >= 0` pero un resultado de
  una posición cerrada siempre vendió todo).

Notas de campos (decisiones del usuario del 2026-10-05, ver
`app.domain.rules.results`): con salidas parciales `exit_price` es el
promedio ponderado por cantidad y `exit_order_id` / `exit_time` /
`exit_reason` son los de la orden que cerró la posición.
`initial_risk_score` e `initial_confidence_score` quedan `NULL`: el sistema
solo produce un score numérico (`puntaje_ponderado`, que alimenta
`initial_opportunity_score`); riesgo y confianza son cualitativos
(`decision_snapshot`).

Dinero y precios en `NUMERIC`, fechas en `TIMESTAMPTZ` (UTC), UUID.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Enum, ForeignKey, Numeric, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.enums import EstadoTesis, IntencionOrden


class TradeResultModel(Base):
    __tablename__ = "trade_results"
    __table_args__ = (
        CheckConstraint("exit_time >= entry_time", name="tiempos"),
        CheckConstraint("entry_quantity > 0 AND exit_quantity > 0", name="cantidades"),
        CheckConstraint("exit_quantity = entry_quantity", name="posicion_cerrada_del_todo"),
        CheckConstraint("entry_price > 0 AND exit_price > 0", name="precios_positivos"),
        CheckConstraint("side = 'LONG'", name="solo_largos"),
        CheckConstraint("exit_reason <> 'ENTRY'", name="exit_reason_es_salida"),
        CheckConstraint("duration_seconds >= 0", name="duracion_no_negativa"),
        CheckConstraint("total_fees = entry_fee + exit_fee", name="total_fees_coherente"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    position_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("posiciones.id"), nullable=False, unique=True
    )
    entry_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ordenes.id"))
    exit_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ordenes.id"))
    symbol: Mapped[str] = mapped_column(nullable=False)
    side: Mapped[str] = mapped_column(nullable=False, server_default="LONG")
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entry_quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    exit_quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    capital_at_entry: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    position_value_at_entry: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    gross_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    gross_return_pct: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    entry_fee: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, server_default="0")
    exit_fee: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, server_default="0")
    total_fees: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, server_default="0")
    estimated_entry_slippage_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    estimated_exit_slippage_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    estimated_total_slippage: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    net_return_pct: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    initial_stop_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    initial_target_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    initial_risk_per_unit: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    initial_risk_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    realized_r_multiple: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    mae_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    mae_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    mfe_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    mfe_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    analysis_cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ciclos_analisis.id")
    )
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("propuestas.id"))
    initial_opportunity_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    initial_risk_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    initial_confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    thesis_status: Mapped[EstadoTesis] = mapped_column(
        Enum(EstadoTesis, name="estado_tesis", native_enum=False, length=30, validate_strings=True),
        nullable=False,
    )
    exit_reason: Mapped[IntencionOrden] = mapped_column(
        Enum(IntencionOrden, name="intencion_orden", native_enum=False, length=32, validate_strings=True),
        nullable=False,
    )
    btc_return_during_trade_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    market_return_during_trade_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
