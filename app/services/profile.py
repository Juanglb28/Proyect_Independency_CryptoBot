"""Perfil de inversión (Paso 10b): lectura del perfil vigente. Solo lectura.

El perfil es INFORMATIVO: se muestra, no se usa para decidir nada.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.repositories.perfil_usuario import count_perfiles, get_ultimo_perfil


@dataclass(frozen=True)
class PerfilGuardado:
    creado_en: datetime
    horizonte_meses: int
    perdida_maxima_pct: Decimal
    experiencia: str
    activos_excluidos: tuple[str, ...]
    tolerancia_declarada: str
    objetivo_rentabilidad_pct: Decimal
    objetivo_es_realista: bool
    disponibilidad_aprobacion: str
    pct_patrimonio: Decimal
    contradicciones_detectadas: tuple[str, ...]
    confianza_perfil: str
    modelo: str
    prompt_version: str


@dataclass(frozen=True)
class PerfilView:
    vigente: PerfilGuardado | None
    total_perfiles: int


def get_perfil_view(session: Session) -> PerfilView:
    p = get_ultimo_perfil(session)
    if p is None:
        return PerfilView(None, 0)
    return PerfilView(
        PerfilGuardado(
            creado_en=p.created_at,
            horizonte_meses=p.horizonte_meses,
            perdida_maxima_pct=p.perdida_maxima_pct,
            experiencia=p.experiencia,
            activos_excluidos=tuple(p.activos_excluidos),
            tolerancia_declarada=p.tolerancia_declarada,
            objetivo_rentabilidad_pct=p.objetivo_rentabilidad_pct,
            objetivo_es_realista=p.objetivo_es_realista,
            disponibilidad_aprobacion=p.disponibilidad_aprobacion,
            pct_patrimonio=p.pct_patrimonio,
            contradicciones_detectadas=tuple(p.contradicciones_detectadas),
            confianza_perfil=p.confianza_perfil,
            modelo=p.modelo,
            prompt_version=p.prompt_version,
        ),
        count_perfiles(session),
    )
