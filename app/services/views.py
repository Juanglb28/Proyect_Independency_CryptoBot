"""Fachada que usa la interfaz: una función por vista, sin argumentos de base de
datos. Cada una abre su propia sesión de SOLO LECTURA (`read_session`), toma la
hora actual en UTC y la configuración, y devuelve datos listos para mostrar.

La interfaz importa únicamente este módulo (y los tipos de datos de
`app.services.*`); nunca una sesión ni un repositorio.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.services.dashboard import DashboardView, get_dashboard
from app.services.decisions import CicloDetalle, CicloResumen, get_ciclo_detalle, list_ciclos
from app.services.performance import PerformanceView, get_performance_view
from app.services.positions import PosicionesView, get_positions_view
from app.services.session import read_session, settings
from app.services.system import SistemaView, get_system_view
from app.services.trades import (
    FiltroOperaciones,
    OperacionDetalle,
    OperacionFila,
    get_operacion_detalle,
    list_operaciones,
)
from app.services.validation import ValidationView, get_validation_view


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def dashboard() -> DashboardView:
    with read_session() as s:
        return get_dashboard(s, initial_capital=settings().paper_capital_usd, now=_ahora())


def posiciones() -> PosicionesView:
    with read_session() as s:
        return get_positions_view(s, initial_capital=settings().paper_capital_usd, now=_ahora())


def operaciones(filtro: FiltroOperaciones | None = None) -> tuple[OperacionFila, ...]:
    with read_session() as s:
        return list_operaciones(s, filtro)


def operacion_detalle(position_id: uuid.UUID) -> OperacionDetalle | None:
    with read_session() as s:
        return get_operacion_detalle(s, position_id)


def performance(days: int | None = None) -> PerformanceView:
    with read_session() as s:
        return get_performance_view(s, initial_capital=settings().paper_capital_usd, now=_ahora(), days=days)


def validacion() -> ValidationView:
    with read_session() as s:
        return get_validation_view(s, initial_capital=settings().paper_capital_usd, now=_ahora())


def ciclos(limit: int = 30) -> tuple[CicloResumen, ...]:
    with read_session() as s:
        return list_ciclos(s, limit=limit)


def ciclo_detalle(ciclo_id: uuid.UUID) -> CicloDetalle | None:
    with read_session() as s:
        return get_ciclo_detalle(s, ciclo_id)


def sistema() -> SistemaView:
    cfg = settings()
    with read_session() as s:
        return get_system_view(
            s,
            initial_capital=cfg.paper_capital_usd,
            now=_ahora(),
            saldo_declarado_usd=cfg.anthropic_saldo_declarado_usd,
            saldo_declarado_en=cfg.anthropic_saldo_declarado_en,
        )
