"""Persistencia de `performance_metrics` (Paso 9; sección 8.6 del plan).

Esta capa SOLO persiste (sección 15.2): las métricas ya vienen calculadas por
`app/domain/rules/results.py`. A diferencia de `trade_results`, esta tabla es
DERIVADA y reconstruible, así que recalcular un mismo alcance y periodo
REEMPLAZA su fila (`upsert_performance`).

El reemplazo se hace bajo un candado de asesor de Postgres a nivel de
transacción (reentrante en la misma sesión): dos recálculos simultáneos del
mismo alcance no pueden insertar dos filas ni pisarse a medias. La clave
única de la tabla usa `coalesce(scope_key, '')` para que `GLOBAL`
(`scope_key` NULL) también sea único por periodo.

La transacción la maneja quien llama.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select, text, update
from sqlalchemy.orm import Session

from app.db.models.performance import PerformanceMetric
from app.domain.enums import AlcanceMetricas

_LOCK_KEY_PERFORMANCE = 8_200_410_003
_CLAVE = ("scope_type", "scope_key", "period_start", "period_end")
_COLUMNAS = frozenset(c.name for c in PerformanceMetric.__table__.columns) - {"id"}


def _clave_igual(scope_type: AlcanceMetricas, scope_key: str | None, period_start: datetime, period_end: datetime):
    return and_(
        PerformanceMetric.scope_type == scope_type,
        PerformanceMetric.scope_key.is_(None) if scope_key is None else PerformanceMetric.scope_key == scope_key,
        PerformanceMetric.period_start == period_start,
        PerformanceMetric.period_end == period_end,
    )


def get_performance(
    session: Session,
    *,
    scope_type: AlcanceMetricas,
    scope_key: str | None,
    period_start: datetime,
    period_end: datetime,
) -> PerformanceMetric | None:
    return session.execute(
        select(PerformanceMetric).where(_clave_igual(scope_type, scope_key, period_start, period_end))
    ).scalar_one_or_none()


def list_performance(session: Session, *, scope_type: AlcanceMetricas | None = None) -> list[PerformanceMetric]:
    stmt = select(PerformanceMetric).order_by(
        PerformanceMetric.period_start, PerformanceMetric.period_end, PerformanceMetric.scope_type,
        PerformanceMetric.scope_key, PerformanceMetric.id,
    )
    if scope_type is not None:
        stmt = stmt.where(PerformanceMetric.scope_type == scope_type)
    return list(session.execute(stmt).scalars())


def upsert_performance(session: Session, valores: Mapping[str, Any]) -> tuple[int, bool]:
    """Guarda las métricas de un alcance y periodo. Devuelve `(id, creada)`;
    `creada=False` si reemplazó una fila existente (recálculo)."""
    desconocidas = set(valores) - _COLUMNAS
    if desconocidas:
        raise ValueError(f"columnas desconocidas para performance_metrics: {sorted(desconocidas)}")
    faltan = [k for k in _CLAVE if k not in valores]
    if faltan:
        raise ValueError(f"faltan campos de la clave de performance_metrics: {faltan}")

    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY_PERFORMANCE})
    existente = get_performance(
        session,
        scope_type=valores["scope_type"],
        scope_key=valores["scope_key"],
        period_start=valores["period_start"],
        period_end=valores["period_end"],
    )
    if existente is None:
        fila = PerformanceMetric(**valores)
        session.add(fila)
        session.flush()
        return fila.id, True
    session.execute(
        update(PerformanceMetric).where(PerformanceMetric.id == existente.id).values(**valores)
    )
    session.expire(existente)
    return existente.id, False
