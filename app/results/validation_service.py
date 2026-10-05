"""Servicio de validación de la capacidad predictiva (Paso 9; sección 8.7).

Carga los `trade_results` del periodo y se los entrega a la función pura
`validate_score_discrimination` (bootstrap con semilla fija). No guarda nada:
el informe se recalcula cuando se pide y es reproducible, porque depende solo
de los resultados (inmutables) y de la semilla.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.domain.rules.results import ValidationReport, validate_score_discrimination
from app.results.performance_service import load_trades


def validate_score(
    session: Session, *, period_start: datetime | None = None, period_end: datetime | None = None
) -> ValidationReport:
    return validate_score_discrimination(load_trades(session, period_start=period_start, period_end=period_end))
