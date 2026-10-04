"""Modelo ORM de `eventos_sistema` (Paso 8, decisión del usuario del
2026-10-04): registro de SOLO-AGREGAR de las pausas por drawdown y sus
reanudaciones.

Por qué existe: el plan no trae un lugar para el estado "el sistema está
pausado por drawdown", y P5 exige que ese estado sobreviva a un reinicio (un
sistema que arranca "despausado" por haberse caído durante una pausa abriría
entradas que no debía). Se eligió un registro de eventos en vez de un campo
que se sobrescribe para conservar la historia completa (cuándo se pausó, con
qué equity y pico, cuándo y con qué equity se reanudó).

Estado actual = tipo del ÚLTIMO evento (`id` más alto): sin eventos o último
`REANUDACION` -> no pausado; último `PAUSA_DRAWDOWN` -> pausado.

Garantías que vive en la base de datos:

- Solo-agregar: un trigger rechaza `UPDATE` y `DELETE` (la historia de
  pausas no se reescribe — mismo espíritu que la sección 14 del plan).
- Una `REANUDACION` siempre trae el pico anterior y fija el pico nuevo igual
  al equity de ese momento (`ck_eventos_sistema_reanudacion_reinicia_pico`).
- Solo una `REANUDACION` puede traer pico anterior.

El orden lo da `id` (identidad de Postgres), no el reloj: `occurred_at` es la
hora del suceso según quien lo registra y podría empatar o retroceder; `id`
sí es monotónico (los inserts se serializan en el repositorio).

Dinero en `NUMERIC`, fechas en `TIMESTAMPTZ` (UTC) — nunca `float`
(sección 15.3 del plan). No lleva `float` ni el umbral de -15%: decidir
cuándo pausar es de `app/domain/rules/risk.py`, aquí solo se persiste.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Enum, Identity, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.enums import TipoEventoSistema


class EventoSistema(Base):
    __tablename__ = "eventos_sistema"
    __table_args__ = (
        CheckConstraint("equity_usd >= 0", name="equity_no_negativo"),
        CheckConstraint("peak_equity_usd > 0", name="pico_positivo"),
        CheckConstraint(
            "previous_peak_equity_usd IS NULL OR previous_peak_equity_usd > 0",
            name="pico_previo_positivo",
        ),
        CheckConstraint(
            "event_type <> 'REANUDACION' OR "
            "(previous_peak_equity_usd IS NOT NULL AND peak_equity_usd = equity_usd)",
            name="reanudacion_reinicia_pico",
        ),
        CheckConstraint(
            "event_type = 'REANUDACION' OR previous_peak_equity_usd IS NULL",
            name="solo_reanudacion_tiene_pico_previo",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_type: Mapped[TipoEventoSistema] = mapped_column(
        Enum(TipoEventoSistema, name="tipo_evento_sistema", native_enum=False, length=24, validate_strings=True),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    equity_usd: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    """Equity (capital + P&L realizado y no realizado) al ocurrir el evento."""
    peak_equity_usd: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    """Pico de equity vigente DESPUÉS del evento. En una pausa es el pico
    contra el que se midió el -15%; en una reanudación es el pico nuevo, igual
    al equity de ese momento."""
    previous_peak_equity_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    """Solo en `REANUDACION`: el pico que había antes de reiniciarlo."""
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
