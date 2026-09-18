"""Modelo ORM de `eventos` (sección 6 del plan).

Catálogo curado de eventos macro (40-60 eventos, construido manualmente en
Fase 2 — ver `scripts/seed_events.py`, todavía no implementado). Este
entregable solo define el esquema; no se siembra ningún dato aquí.

Usa UUID como identificador porque es la convención del proyecto para
entidades que se referencian desde otras tablas (`analisis_eventos`, todavía
no implementada).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Evento(Base):
    __tablename__ = "eventos"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    event_type: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str | None] = mapped_column(nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
