"""Persistencia de `perfil_usuario` (Paso 10b). Solo persiste: no interpreta
ni usa el perfil para nada (es informativo; ver el modelo). La transacción la
maneja quien llama."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.perfil_usuario import PerfilUsuario


@dataclass(frozen=True)
class PerfilRow:
    horizonte_meses: int
    perdida_maxima_pct: Decimal
    experiencia: str
    activos_excluidos: list[str]
    tolerancia_declarada: str
    objetivo_rentabilidad_pct: Decimal
    objetivo_es_realista: bool
    disponibilidad_aprobacion: str
    pct_patrimonio: Decimal
    contradicciones_detectadas: list[str]
    confianza_perfil: str
    modelo: str
    prompt_version: str


def save_perfil(session: Session, row: PerfilRow) -> int:
    """Agrega un perfil nuevo y devuelve su `id`."""
    perfil = PerfilUsuario(
        horizonte_meses=row.horizonte_meses,
        perdida_maxima_pct=row.perdida_maxima_pct,
        experiencia=row.experiencia,
        activos_excluidos=list(row.activos_excluidos),
        tolerancia_declarada=row.tolerancia_declarada,
        objetivo_rentabilidad_pct=row.objetivo_rentabilidad_pct,
        objetivo_es_realista=row.objetivo_es_realista,
        disponibilidad_aprobacion=row.disponibilidad_aprobacion,
        pct_patrimonio=row.pct_patrimonio,
        contradicciones_detectadas=list(row.contradicciones_detectadas),
        confianza_perfil=row.confianza_perfil,
        modelo=row.modelo,
        prompt_version=row.prompt_version,
    )
    session.add(perfil)
    session.flush()
    return perfil.id


def get_ultimo_perfil(session: Session) -> PerfilUsuario | None:
    """El perfil vigente: la fila más reciente."""
    return session.execute(select(PerfilUsuario).order_by(PerfilUsuario.id.desc()).limit(1)).scalar_one_or_none()


def count_perfiles(session: Session) -> int:
    return len(session.execute(select(PerfilUsuario.id)).scalars().all())
