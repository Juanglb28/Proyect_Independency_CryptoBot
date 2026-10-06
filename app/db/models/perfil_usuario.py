"""Modelo ORM de `perfil_usuario` (Paso 10b; decisión del usuario, 2026-10-06):
el perfil que arma el Agente 1 (Entrevistador), una fila por entrevista.

Es INFORMATIVO: ningún análisis, score ni regla de riesgo lo lee (decisión
del usuario). El perfil vigente es la fila más reciente; las anteriores se
conservan como historia.

Garantías en la base de datos:

- Solo-agregar: un trigger rechaza `UPDATE` y `DELETE` (un perfil no se
  reescribe; si cambia, se hace otra entrevista).
- Rangos coherentes con el schema del agente (`PerfilUsuarioOutput`).

No se guardan las respuestas textuales del usuario, solo el perfil final que
define el plan (sección 7.2). Porcentajes en `NUMERIC`, fechas `TIMESTAMPTZ`.
Es una tabla adicional a las 13 del master prompt, aprobada por el usuario.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Identity, Integer, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PerfilUsuario(Base):
    __tablename__ = "perfil_usuario"
    __table_args__ = (
        CheckConstraint("horizonte_meses > 0", name="horizonte_positivo"),
        CheckConstraint("perdida_maxima_pct >= 0 AND perdida_maxima_pct <= 100", name="perdida_en_rango"),
        CheckConstraint("pct_patrimonio >= 0 AND pct_patrimonio <= 100", name="patrimonio_en_rango"),
        CheckConstraint("objetivo_rentabilidad_pct >= 0", name="objetivo_no_negativo"),
        CheckConstraint("experiencia IN ('ninguna', 'basica', 'intermedia', 'avanzada')", name="experiencia_valida"),
        CheckConstraint("confianza_perfil IN ('alta', 'media', 'baja')", name="confianza_valida"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)

    horizonte_meses: Mapped[int] = mapped_column(Integer, nullable=False)
    perdida_maxima_pct: Mapped[Decimal] = mapped_column(Numeric(7, 2), nullable=False)
    experiencia: Mapped[str] = mapped_column(nullable=False)
    activos_excluidos: Mapped[list] = mapped_column(JSONB, nullable=False)
    tolerancia_declarada: Mapped[str] = mapped_column(Text, nullable=False)
    objetivo_rentabilidad_pct: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    objetivo_es_realista: Mapped[bool] = mapped_column(Boolean, nullable=False)
    disponibilidad_aprobacion: Mapped[str] = mapped_column(Text, nullable=False)
    pct_patrimonio: Mapped[Decimal] = mapped_column(Numeric(7, 2), nullable=False)
    contradicciones_detectadas: Mapped[list] = mapped_column(JSONB, nullable=False)
    confianza_perfil: Mapped[str] = mapped_column(nullable=False)

    modelo: Mapped[str] = mapped_column(nullable=False)
    prompt_version: Mapped[str] = mapped_column(nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
