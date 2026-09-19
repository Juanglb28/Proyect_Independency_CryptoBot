"""Modelo ORM de `velas` (sección 6 del plan).

Hipertabla de TimescaleDB particionada por `open_time` (la conversión a
hipertabla se hace en la migración, no aquí — SQLAlchemy solo conoce una
tabla Postgres normal).

Restricción única lógica (sección 15.7 del plan): `symbol + timeframe +
open_time`. Esto es lo que garantiza que la ingesta pueda reintentarse sin
duplicar velas.

Todos los timestamps son TIMESTAMPTZ en UTC (sección 15.3); todos los precios
y volúmenes son NUMERIC, nunca float, para no perder precisión en cálculos
financieros posteriores.

Clave primaria compuesta `(id, open_time)`: requisito de TimescaleDB, no una
elección de diseño de negocio. `create_hypertable()` exige que toda columna
única (incluida la primary key) incluya la columna de partición
(`open_time`); una PK de solo `id` hace que la conversión a hipertable falle
con "cannot create a unique index without the column open_time (used in
partitioning)". `id` sigue siendo único en la práctica porque lo genera una
secuencia, pero esa unicidad ya no está reforzada por un constraint propio
(ninguna otra tabla referencia `velas.id` por FK todavía, así que no hay
impacto). Ver documentación de TimescaleDB sobre hypertables con PK/UNIQUE.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Numeric, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Vela(Base):
    __tablename__ = "velas"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "open_time", name="uq_velas_symbol_timeframe_open_time"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(nullable=False)
    timeframe: Mapped[str] = mapped_column(nullable=False)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, nullable=False)

    open: Mapped[float] = mapped_column(Numeric(30, 12), nullable=False)
    high: Mapped[float] = mapped_column(Numeric(30, 12), nullable=False)
    low: Mapped[float] = mapped_column(Numeric(30, 12), nullable=False)
    close: Mapped[float] = mapped_column(Numeric(30, 12), nullable=False)
    volume: Mapped[float] = mapped_column(Numeric(30, 12), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
