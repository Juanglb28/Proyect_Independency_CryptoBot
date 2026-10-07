"""Modelo ORM de `alertas` (Paso 11a; tabla aprobada por el usuario el 2026-10-07).

Cada fila es UN aviso para el usuario (Telegram). Son solo avisos: ninguna
decisión de trading depende de esta tabla, salvo el estado de "Binance caído"
(decisión D5), que es el tipo de la última alerta del par
`BINANCE_CAIDO`/`BINANCE_RESTABLECIDO`.

Por qué existe: los avisos deben sobrevivir a un reinicio (P5) y no repetirse.
- `dedup_key` es UNIQUE: la decide quien crea el aviso, de forma determinista,
  para que un reintento (o un reinicio a mitad de un chequeo) no cree un
  segundo aviso del mismo episodio.
- Se guarda el MENSAJE ya armado y el valor observado frente al umbral, para
  poder auditar después por qué se avisó.

Garantías que vive en la base de datos (trigger `trg_alertas_inmutables`):

- Nunca se borra ni se reescribe el CONTENIDO de un aviso.
- Solo cambia el "recibo de entrega" (`delivered_at`, `delivery_attempts`,
  `last_delivery_error`), y solo hacia adelante: un aviso entregado no vuelve
  a "no entregado" y los intentos no bajan. Así se pueden reintentar los
  envíos fallidos sin duplicar ni perder avisos.

Dinero y magnitudes en `NUMERIC`, fechas en `TIMESTAMPTZ` (UTC), nunca `float`
(sección 15.3 del plan).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.enums import TipoAlerta


class Alerta(Base):
    __tablename__ = "alertas"
    __table_args__ = (
        CheckConstraint("delivery_attempts >= 0", name="intentos_no_negativos"),
        CheckConstraint("delivered_at IS NULL OR delivery_attempts >= 1", name="entregada_implica_intento"),
        Index(
            "ix_alertas_pendientes_de_entrega",
            "id",
            postgresql_where=text("delivered_at IS NULL"),
        ),
        Index("ix_alertas_tipo_id", "tipo", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    tipo: Mapped[TipoAlerta] = mapped_column(
        Enum(TipoAlerta, name="tipo_alerta", native_enum=False, length=32, validate_strings=True),
        nullable=False,
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    """De qué trata: un símbolo (`ETHUSDT`) o `SISTEMA`."""
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    """Clave única del episodio (ver el docstring del módulo)."""
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    """Texto exacto que se envía. Lo arma código determinista con datos
    guardados; nunca un modelo de lenguaje."""
    observed_value: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    threshold: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("posiciones.id"), nullable=True
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ordenes.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # --- recibo de entrega (lo único que se actualiza) ---
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_delivery_error: Mapped[str | None] = mapped_column(Text, nullable=True)
