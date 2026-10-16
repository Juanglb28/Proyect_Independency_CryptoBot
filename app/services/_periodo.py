"""Periodos que usan varias vistas (solo lectura)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.domain.enums import SerieOperacion
from app.repositories.equity import list_observaciones
from app.results.performance_service import history_start


def inicio_del_historial(
    session: Session, *, now: datetime, serie: SerieOperacion | None = SerieOperacion.ESTRATEGIA
) -> datetime:
    """Inicio del historial completo: lo más antiguo entre la primera entrada
    y la primera observación de equity (el monitor puede llevar días vigilando
    antes de la primera operación y esa curva también cuenta)."""
    inicio = history_start(session, now=now, serie=serie)
    observaciones = list_observaciones(session)
    if observaciones and serie == SerieOperacion.ESTRATEGIA:
        inicio = min(inicio, observaciones[0].observed_at)
    return inicio


def inicio_de_periodo(
    session: Session, *, now: datetime, days: int | None, serie: SerieOperacion | None = SerieOperacion.ESTRATEGIA
) -> datetime:
    """`days=None` -> historial completo; si no, los últimos `days` días."""
    if days is None:
        return inicio_del_historial(session, now=now, serie=serie)
    if days <= 0:
        raise ValueError("days debe ser positivo")
    return now - timedelta(days=days)
