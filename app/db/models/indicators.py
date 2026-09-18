"""Modelo ORM de `indicadores` (sección 6 del plan).

Diseño deliberadamente genérico (una fila por indicador calculado, en vez de
una columna por indicador): la Fase 1 todavía no ha fijado el conjunto final
de indicadores más allá de los parámetros ya decididos en la sección 6.1 del
plan (RSI-14, ATR-14, SMA 90/120/180, volumen relativo, distancia a ATH/90d).
Un esquema ancho hoy obligaría a una migración por cada indicador nuevo; este
esquema EAV evita comprometerse a columnas específicas antes de tiempo, sin
violar la regla de "no sobreimplementación" (sección 29 del master prompt).

Restricción única: `symbol + timeframe + calculated_at + indicator_name`,
misma lógica de idempotencia que `velas`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Numeric, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Indicador(Base):
    __tablename__ = "indicadores"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "timeframe",
            "calculated_at",
            "indicator_name",
            name="uq_indicadores_symbol_timeframe_calculated_at_indicator_name",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(nullable=False)
    timeframe: Mapped[str] = mapped_column(nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    indicator_name: Mapped[str] = mapped_column(nullable=False)
    value: Mapped[float] = mapped_column(Numeric(30, 12), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
