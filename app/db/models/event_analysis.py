"""Modelo ORM de `analisis_eventos` (sección 6 del plan, Paso 5).

"Comportamiento del precio en ventanas alrededor de cada evento": para cada
combinación (evento, símbolo, ventana), cuánto cambió el precio de cierre
diario entre la fecha del evento y `event_date + window_offset_days`.

Ventanas fijadas (decisión del usuario, 2026-09-29, ver
`app/data/event_analyzer.py::WINDOW_OFFSETS_DAYS`): -7, -1, +1, +3, +7, +30
días calendario. Las negativas (-7, -1) son precio ANTES del evento —
contexto, no una predicción; las positivas son la reacción posterior, que
es precisamente lo que Agente 3 (analista de eventos) usa para juzgar
semejanza con la situación actual. Construir esta tabla no es look-ahead
bias: los eventos del catálogo ya sucedieron por completo, así que usar su
reacción posterior para caracterizarlos —de cara a comparar un evento
FUTURO o presente contra este historial— es exactamente el diseño previsto
en la sección 4.2 del plan, no una fuga de información hacia una decisión
pasada.

`reference_price`/`window_price` usan el cierre diario más cercano por
debajo (nunca por encima) de la fecha objetivo, para no mezclar ambas
direcciones de redondeo entre eventos: ver
`app/data/event_analyzer.py::_closest_close_on_or_before`. Si no hay ninguna
vela en o antes de la fecha objetivo (el evento es más antiguo que el
historial de velas ingerido, ~2 años — sección 3 del plan), la combinación
se declara dato faltante y no se inserta ninguna fila (sección P3).

Clave de idempotencia: `UNIQUE(evento_id, symbol, window_offset_days)`,
mismo patrón que `indicadores`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Numeric, SmallInteger, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AnalisisEvento(Base):
    __tablename__ = "analisis_eventos"
    __table_args__ = (
        UniqueConstraint(
            "evento_id", "symbol", "window_offset_days", name="uq_analisis_eventos_evento_symbol_window"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    evento_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("eventos.id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(nullable=False)
    window_offset_days: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    reference_date: Mapped[date] = mapped_column(Date, nullable=False)
    reference_price: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    window_date: Mapped[date] = mapped_column(Date, nullable=False)
    window_price: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    price_change_pct: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
