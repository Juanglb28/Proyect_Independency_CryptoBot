"""Modelo ORM de `eventos` (sección 6 del plan, Paso 5).

Catálogo curado de eventos macro (40-60 eventos, sección 3 del plan:
"Datos de eventos: Catálogo manual curado"). El contenido real (qué eventos,
qué fecha, qué severidad) no lo decide este código — lo cura el usuario a
partir de un borrador investigado con fuentes reales, y se carga con
`scripts/seed_events.py` desde `data/events/*.json` (sección P3 del plan:
cero datos inventados).

Usa UUID como identificador porque es la convención del proyecto para
entidades que se referencian desde otras tablas (`analisis_eventos`).

`external_id` (agregado en la migración de Paso 5) es la clave de
idempotencia para el seed: un slug legible y estable que el propio archivo
de catálogo asigna a cada evento (p. ej. "btc-halving-2024"), independiente
del UUID interno. A diferencia de `velas`/`indicadores` — hechos medidos
que nunca se alteran retroactivamente —, `eventos` es contenido curado que
el usuario puede corregir (una fecha mal tipeada, una fuente rota): por eso
`save_events` hace upsert (`ON CONFLICT ... DO UPDATE`) sobre `external_id`
en vez de `DO NOTHING` — ver `app/repositories/events.py`.

`severity` y `scope` quedan como texto libre (igual filosofía que el
esquema EAV de `indicadores`: no comprometerse a un enum antes de tener el
catálogo real). Convención documentada, no impuesta por la base de datos:
`severity` en {alta, media, baja} (mismo vocabulario que el resto del plan
para confianza/semejanza); `scope` en "GLOBAL" o una lista de símbolos de
`ALLOWED_SYMBOLS` separados por coma (p. ej. "ETHUSDT") — ver
`app/data/event_analyzer.py`, que interpreta este campo.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Evento(Base):
    __tablename__ = "eventos"
    __table_args__ = (UniqueConstraint("external_id", name="uq_eventos_external_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    external_id: Mapped[str] = mapped_column(nullable=False)

    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    event_type: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str | None] = mapped_column(nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
