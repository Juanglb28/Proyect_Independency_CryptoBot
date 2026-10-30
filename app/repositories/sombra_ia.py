"""Lectura para el estudio S1 (métrica a la sombra del análisis de IA).

Solo persistencia, SOLO LECTURA: devuelve filas planas del dominio
(`app.domain.rules.sombra_ia`) y no calcula nada. Quien lo use debe abrir la sesión con
`set_transaction_read_only` (el script del estudio lo hace). La regla de qué es un
«incluido» o un «candidato» vive en el dominio, no aquí.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.ai_calls import LlamadaIA
from app.db.models.analysis import CicloAnalisis, PrefiltroResultado, Propuesta

AGENTE_CONSOLIDADOR = "consolidador"


@dataclass(frozen=True)
class CicloConConsolidador:
    ciclo_id: str
    started_at: datetime
    recomendacion: str | None  # de la respuesta guardada del Consolidador
    prompt_version: str | None


@dataclass(frozen=True)
class FilaPrefiltroS1:
    ciclo_id: str
    symbol: str
    estado: str
    score_final: Decimal | None


@dataclass(frozen=True)
class PropuestaS1:
    ciclo_id: str
    symbol: str
    tier: str | None
    puntaje_ponderado: Decimal


def leer_ciclos_con_consolidador(session: Session) -> list[CicloConConsolidador]:
    """Ciclos con al menos una llamada EXITOSA al Consolidador. Si un ciclo tiene varias
    (reintentos), se usa la más reciente. `recomendacion` sale de la respuesta guardada."""
    filas = session.execute(
        select(
            LlamadaIA.ciclo_id,
            LlamadaIA.respuesta,
            LlamadaIA.prompt_version,
            LlamadaIA.created_at,
            CicloAnalisis.started_at,
        )
        .join(CicloAnalisis, CicloAnalisis.id == LlamadaIA.ciclo_id)
        .where(LlamadaIA.agente == AGENTE_CONSOLIDADOR, LlamadaIA.exitosa.is_(True))
        .order_by(LlamadaIA.created_at)
    ).all()
    ultimo: dict = {}
    for f in filas:  # orden ascendente: la última escritura gana
        ultimo[f.ciclo_id] = f
    return [
        CicloConConsolidador(
            ciclo_id=str(f.ciclo_id),
            started_at=f.started_at,
            recomendacion=(f.respuesta or {}).get("recomendacion"),
            prompt_version=f.prompt_version,
        )
        for f in ultimo.values()
    ]


def leer_prefiltro(session: Session) -> list[FilaPrefiltroS1]:
    filas = session.execute(
        select(
            PrefiltroResultado.ciclo_id,
            PrefiltroResultado.symbol,
            PrefiltroResultado.estado,
            PrefiltroResultado.score_final,
        )
    ).all()
    return [FilaPrefiltroS1(str(f.ciclo_id), f.symbol, f.estado.value, f.score_final) for f in filas]


def leer_propuestas(session: Session) -> list[PropuestaS1]:
    filas = session.execute(
        select(Propuesta.ciclo_id, Propuesta.symbol, Propuesta.tier_entrada, Propuesta.puntaje_ponderado)
    ).all()
    return [
        PropuestaS1(str(f.ciclo_id), f.symbol, None if f.tier_entrada is None else f.tier_entrada.value, f.puntaje_ponderado)
        for f in filas
    ]
