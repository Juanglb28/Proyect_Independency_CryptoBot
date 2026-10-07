"""Servicio del análisis por factor (sección 8.8 del plan; `factor_analysis`).

Carga las operaciones cerradas y los factores guardados de la propuesta que
las originó, y se los entrega a la función pura `analyze_factors`. No guarda
nada: el informe se recalcula al pedirlo y es reproducible, porque depende
solo de datos inmutables (resultados y factores congelados antes de entrar)
y de la semilla fija.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.domain.rules.factors import FactorAnalysisReport, FactorDeOperacion, analyze_factors
from app.repositories.analysis import list_factores_de_propuestas
from app.results.performance_service import load_trades


def analyze_factors_for_period(
    session: Session, *, period_start: datetime | None = None, period_end: datetime | None = None
) -> FactorAnalysisReport:
    trades = load_trades(session, period_start=period_start, period_end=period_end)
    posiciones_de_propuesta: dict[uuid.UUID, list[uuid.UUID]] = {}
    for t in trades:
        if t.proposal_id is not None:
            posiciones_de_propuesta.setdefault(t.proposal_id, []).append(t.position_id)
    filas = list_factores_de_propuestas(session, list(posiciones_de_propuesta))
    factores = [
        FactorDeOperacion(
            position_id=position_id, factor=f.factor, peso=f.peso, puntaje=f.puntaje, confianza=f.confianza
        )
        for f in filas
        for position_id in posiciones_de_propuesta[f.propuesta_id]
    ]
    return analyze_factors(trades, factores)
