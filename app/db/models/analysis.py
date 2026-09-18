"""Modelo ORM de `ciclos_analisis` (sección 6 del plan).

Registra cada ejecución del ciclo diario (sección 15.6): entrada, salida,
costo, duración. El campo `status` es texto libre por ahora — se formalizará
como enum de dominio cuando se implemente la máquina de estados del
orquestador (`app/orchestration/daily_cycle.py`, todavía no implementado):

    TODO — DECISIÓN PENDIENTE: enum formal de estados del ciclo
    (START, INGEST, VALIDATE_DATA, CALCULATE_INDICATORS, PREFILTER,
    EVENT_CORRELATION, AI_ANALYSIS, CONSOLIDATE, FREEZE_DECISION_SNAPSHOT,
    RISK_VALIDATION, ...) — se define en Fase 3, junto con el orquestador
    que efectivamente transiciona entre esos estados. Introducir el enum
    ahora, sin orquestador que lo use, sería exactamente el tipo de
    implementación arbitraria que el proyecto quiere evitar (sección 29 del
    master prompt).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CicloAnalisis(Base):
    __tablename__ = "ciclos_analisis"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(nullable=False)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
