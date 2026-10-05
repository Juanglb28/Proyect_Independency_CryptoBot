"""Modelo ORM de `performance_metrics` (Paso 9; sección 8.6 del plan):
métricas AGREGADAS, derivadas y reconstruibles. Si se borra esta tabla, se
reconstruye entera desde `trade_results` y `equity_observaciones`; es la
única tabla de resultados que SÍ se actualiza (recalcular reemplaza la fila).

Una fila por `(scope_type, scope_key, period_start, period_end)`: recalcular
el mismo alcance y periodo actualiza esa fila (`calculated_at` dice cuándo).
`scope_key` es `NULL` solo en `GLOBAL`. `max_drawdown_*` y las columnas de
caída se guardan NEGATIVAS (o 0), como en la fórmula del plan.

Dinero en `NUMERIC`, fechas en `TIMESTAMPTZ` (UTC).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Enum, Identity, Index, Integer, Numeric, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.enums import AlcanceMetricas


def _n12() -> Mapped[Decimal | None]:
    return mapped_column(Numeric(12, 6), nullable=True)


def _usd() -> Mapped[Decimal | None]:
    return mapped_column(Numeric(20, 8), nullable=True)


class PerformanceMetric(Base):
    __tablename__ = "performance_metrics"
    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="periodo"),
        CheckConstraint(
            "(scope_type = 'GLOBAL' AND scope_key IS NULL) OR (scope_type <> 'GLOBAL' AND scope_key IS NOT NULL)",
            name="alcance_coherente",
        ),
        CheckConstraint(
            "total_trades = winning_trades + losing_trades + breakeven_trades", name="conteos_coherentes"
        ),
        CheckConstraint("total_trades >= 0", name="total_no_negativo"),
        Index(
            "uq_performance_metrics_alcance_periodo",
            "scope_type",
            text("coalesce(scope_key, '')"),
            "period_start",
            "period_end",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scope_type: Mapped[AlcanceMetricas] = mapped_column(
        Enum(AlcanceMetricas, name="alcance_metricas", native_enum=False, length=16, validate_strings=True),
        nullable=False,
    )
    scope_key: Mapped[str | None] = mapped_column(nullable=True)
    total_trades: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    winning_trades: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    losing_trades: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    breakeven_trades: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    win_rate_pct: Mapped[Decimal | None] = _n12()
    gross_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, server_default="0")
    total_fees: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, server_default="0")
    total_slippage: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, server_default="0")
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, server_default="0")
    total_return_pct: Mapped[Decimal | None] = _n12()
    average_trade_return_pct: Mapped[Decimal | None] = _n12()
    median_trade_return_pct: Mapped[Decimal | None] = _n12()
    average_winner_pct: Mapped[Decimal | None] = _n12()
    average_loser_pct: Mapped[Decimal | None] = _n12()
    largest_win_pct: Mapped[Decimal | None] = _n12()
    largest_loss_pct: Mapped[Decimal | None] = _n12()
    max_drawdown_pct: Mapped[Decimal | None] = _n12()
    max_drawdown_value: Mapped[Decimal | None] = _usd()
    profit_factor: Mapped[Decimal | None] = _n12()
    expectancy_per_trade: Mapped[Decimal | None] = _usd()
    expectancy_pct: Mapped[Decimal | None] = _n12()
    average_r_multiple: Mapped[Decimal | None] = _n12()
    average_mae_pct: Mapped[Decimal | None] = _n12()
    average_mfe_pct: Mapped[Decimal | None] = _n12()
    average_opportunity_score: Mapped[Decimal | None] = _n12()
    average_risk_score: Mapped[Decimal | None] = _n12()
    average_confidence_score: Mapped[Decimal | None] = _n12()
    high_score_win_rate_pct: Mapped[Decimal | None] = _n12()
    low_score_win_rate_pct: Mapped[Decimal | None] = _n12()
    high_score_expectancy: Mapped[Decimal | None] = _usd()
    low_score_expectancy: Mapped[Decimal | None] = _usd()
    score_discrimination: Mapped[Decimal | None] = _n12()
    benchmark_return_pct: Mapped[Decimal | None] = _n12()
    benchmark_max_drawdown_pct: Mapped[Decimal | None] = _n12()
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
