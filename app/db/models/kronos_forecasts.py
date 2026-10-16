"""Modelo ORM de `kronos_forecasts` (experimento Kronos, enmienda E6 / protocolo K1).

Cada fila es UN pronóstico de Kronos para un símbolo en un instante de
decisión: las N cifras finales de cada trayectoria y su resumen. Es una tabla
de EXPERIMENTO en sombra (P2): sin claves foráneas hacia tablas operativas y
nunca se lee desde ejecución, riesgo, score ni compras.

Garantías:
- Clave única lógica: (símbolo, as_of, revisión de pesos, lookback, horizonte,
  trayectorias, semilla base). Reintentar o reanudar la generación no duplica
  (P5).
- Solo-agregar (trigger `trg_kronos_forecasts_solo_agregar`): un pronóstico
  generado no se modifica ni se borra, como los resultados históricos del
  sistema. Para reiniciar a propósito el experimento: `TRUNCATE kronos_forecasts`
  (los triggers por fila no lo bloquean) o `alembic downgrade`.
- Se guardan las trayectorias (`final_closes`) y todos los parámetros, versiones
  y la huella SHA-256 del protocolo vigente, para poder recalcular cualquier
  resumen y probar con qué reglas se generó.

Magnitudes en `NUMERIC`, fechas en `TIMESTAMPTZ` (UTC), nunca `float`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class KronosForecast(Base):
    __tablename__ = "kronos_forecasts"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "as_of", "weights_revision", "lookback", "horizon", "n_trajectories", "seed",
            name="uq_kronos_forecasts_clave_logica",
        ),
        CheckConstraint("n_trajectories > 0 AND lookback > 0 AND horizon > 0", name="parametros_positivos"),
        CheckConstraint("cardinality(final_closes) = n_trajectories", name="una_cifra_por_trayectoria"),
        CheckConstraint("prob_up >= 0 AND prob_up <= 1", name="prob_up_entre_0_y_1"),
        CheckConstraint("last_close > 0", name="ultimo_cierre_positivo"),
        Index("ix_kronos_forecasts_as_of", "as_of"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """Instante de decisión (00:00 UTC). El contexto solo contiene velas cerradas antes de este instante."""

    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    weights_revision: Mapped[str] = mapped_column(Text, nullable=False)
    tokenizer_id: Mapped[str] = mapped_column(Text, nullable=False)
    tokenizer_revision: Mapped[str] = mapped_column(Text, nullable=False)
    code_commit: Mapped[str] = mapped_column(Text, nullable=False)
    lookback: Mapped[int] = mapped_column(Integer, nullable=False)
    horizon: Mapped[int] = mapped_column(Integer, nullable=False)
    n_trajectories: Mapped[int] = mapped_column(Integer, nullable=False)
    temperature: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    top_p: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """Semilla BASE; la de cada pronóstico se deriva de (base, símbolo, as_of)."""
    uses_volume: Mapped[bool] = mapped_column(Boolean, nullable=False)
    device: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    """Versiones de torch/numpy/pandas (texto JSON): influyen en los números."""
    protocol_sha256: Mapped[str] = mapped_column(Text, nullable=False)

    last_close: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    final_closes: Mapped[list[Decimal]] = mapped_column(ARRAY(Numeric(30, 12)), nullable=False)
    expected_return: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    prob_up: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    dispersion: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
