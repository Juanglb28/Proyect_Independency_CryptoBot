"""Modelo ORM de `trade_results` (Paso 9; sección 8.4 del plan): el resultado
DEFINITIVO de una operación cerrada y fuente histórica primaria de toda
métrica. No depende de la interfaz ni de agregados precalculados.

Garantías que vive en la base de datos:

- **Un solo resultado por posición** (`position_id` único, sección 15.7): un
  reintento del servicio no crea un segundo resultado.
- **Solo-agregar**: un trigger rechaza `UPDATE` y `DELETE`. Un error de cálculo
  se corrige con "una nueva versión/corrección auditable", nunca reescribiendo
  (sección 15.3). Ese mecanismo YA existe (2026-10-13): una corrección es una
  fila nueva que apunta con `supersedes_id` a la que corrige, con un
  `correction_reason` obligatorio; la original queda intacta. La versión
  VIGENTE de una posición es la que ninguna otra corrige. Un índice único
  parcial permite un solo original por posición y una sola corrección por
  versión (cadena lineal); un trigger exige que la corrección sea de la misma
  posición. La corrección SIEMPRE recalcula con `calculate_trade_result`
  (nunca números a mano, §37.8): ver `app/results/trade_result_service.py`.
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

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Enum, ForeignKey, Index, Numeric, Text, func, text
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
        # Corrección auditable (§15.3): el motivo es obligatorio exactamente
        # cuando la fila corrige a otra.
        CheckConstraint(
            "(supersedes_id IS NULL) = (correction_reason IS NULL)", name="motivo_si_corrige"
        ),
        # Un solo ORIGINAL por posición (reemplaza al UNIQUE(position_id) total;
        # conserva la idempotencia de save_trade_result) y una sola CORRECCIÓN
        # por versión (cadena lineal, sin bifurcaciones).
        Index(
            "uq_trade_results_original_por_posicion", "position_id",
            unique=True, postgresql_where=text("supersedes_id IS NULL"),
        ),
        Index(
            "uq_trade_results_una_correccion_por_version", "supersedes_id",
            unique=True, postgresql_where=text("supersedes_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    position_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("posiciones.id"), nullable=False
    )
    # Versionado por encadenamiento (corrección auditable, §15.3): la fila que
    # esta corrige (NULL en el original), su motivo (obligatorio si corrige) y,
    # opcional, quién la ordenó. La versión VIGENTE de una posición es la fila
    # que ninguna otra corrige (ver repositories/trade_results.py).
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade_results.id")
    )
    correction_reason: Mapped[str | None] = mapped_column(Text)
    corrected_by: Mapped[str | None] = mapped_column(Text)
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
