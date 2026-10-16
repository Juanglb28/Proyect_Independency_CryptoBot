"""Servicio de validación de la capacidad predictiva (Paso 9; sección 8.7).

Carga los `trade_results` del periodo y se los entrega a la función pura
`validate_score_discrimination` (bootstrap con semilla fija). No guarda nada:
el informe se recalcula cuando se pide y es reproducible, porque depende solo
de los resultados (inmutables) y de la semilla.

Series (decisión del 2026-10-12): por defecto se valida la serie de ESTRATEGIA,
que es la que mide el plan; la de OBSERVACIÓN (puntaje del pre-filtro) se valida
APARTE pasando `serie=OBSERVACION`, y `serie=None` da la comparación conjunta.
Nunca se mezclan sin decirlo: las escalas del puntaje son distintas.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.domain.enums import SerieOperacion

from app.domain.rules.results import (
    StratifiedValidationReport,
    ValidationReport,
    validate_score_discrimination,
    validate_score_discrimination_stratified,
)
from app.results.performance_service import load_trades


def validate_score(
    session: Session,
    *,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    serie: SerieOperacion | None = SerieOperacion.ESTRATEGIA,
) -> ValidationReport:
    return validate_score_discrimination(
        load_trades(session, period_start=period_start, period_end=period_end, serie=serie)
    )


def validate_score_stratified(
    session: Session,
    *,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    serie: SerieOperacion | None = SerieOperacion.ESTRATEGIA,
) -> StratifiedValidationReport:
    """Medición adicional de la enmienda E4 (centrada por mes de entrada)."""
    return validate_score_discrimination_stratified(
        load_trades(session, period_start=period_start, period_end=period_end, serie=serie)
    )
