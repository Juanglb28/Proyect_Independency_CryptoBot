"""Operaciones cerradas: listado con filtros y detalle (secciones 8.8 y 8.9).

El listado sale de `trade_results` (fuente histórica definitiva) y el detalle
añade lo que había ANTES de entrar (la propuesta con su `decision_snapshot`
congelado y sus factores) y la trayectoria (`trade_snapshots`). Nada se
recalcula con datos de hoy: todo es lo que quedó guardado (regla contra el
look-ahead, sección 8.3).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.enums import EstadoTesis, IntencionOrden, ResultadoOperacion
from app.domain.rules.results import TradeResult, classify_outcome
from app.repositories.analysis import get_factores_de_propuesta, get_propuesta
from app.repositories.snapshots import list_snapshots_for_position
from app.repositories.trade_results import get_trade_result_by_position, list_trade_results
from app.results.trade_result_service import to_domain

MAX_PUNTOS_TRAYECTORIA = 800


@dataclass(frozen=True)
class FiltroOperaciones:
    exit_desde: datetime | None = None
    exit_hasta: datetime | None = None
    symbol: str | None = None
    resultado: ResultadoOperacion | None = None
    score_min: Decimal | None = None
    score_max: Decimal | None = None
    exit_reason: IntencionOrden | None = None
    thesis_status: EstadoTesis | None = None


@dataclass(frozen=True)
class OperacionFila:
    position_id: uuid.UUID
    symbol: str
    entry_time: datetime
    exit_time: datetime
    score: Decimal | None
    entry_price: Decimal
    exit_price: Decimal
    net_return_pct: Decimal
    net_pnl: Decimal
    mae_pct: Decimal | None
    mfe_pct: Decimal | None
    r_multiple: Decimal | None
    thesis_status: EstadoTesis
    exit_reason: IntencionOrden
    resultado: ResultadoOperacion


@dataclass(frozen=True)
class FactorView:
    factor: str
    peso: Decimal
    puntaje: Decimal
    confianza: str | None


@dataclass(frozen=True)
class DecisionView:
    """Lo que el sistema sabía al decidir (propuesta congelada)."""

    proposal_id: uuid.UUID
    cycle_id: uuid.UUID
    categoria: str
    horizonte: str
    nivel_riesgo: str
    puntaje_base: Decimal
    calidad_datos: Decimal
    puntaje_ponderado: Decimal
    confianza_global: str
    tesis: str
    invalidacion: str
    escenario_adverso: dict
    comentario_chartista: str | None
    fuentes: tuple[str, ...]
    decision_snapshot: dict
    factores: tuple[FactorView, ...]


@dataclass(frozen=True)
class PuntoTrayectoria:
    time: datetime
    market_price: Decimal
    unrealized_pnl: Decimal
    unrealized_return_pct: Decimal
    stop_price: Decimal | None
    target_price: Decimal | None
    trailing_active: bool
    system_state: str


@dataclass(frozen=True)
class OperacionDetalle:
    fila: OperacionFila
    resultado: TradeResult
    decision: DecisionView | None
    trayectoria: tuple[PuntoTrayectoria, ...]


def _fila(r: TradeResult) -> OperacionFila:
    return OperacionFila(
        position_id=r.position_id,
        symbol=r.symbol,
        entry_time=r.entry_time,
        exit_time=r.exit_time,
        score=r.initial_opportunity_score,
        entry_price=r.entry_price,
        exit_price=r.exit_price,
        net_return_pct=r.net_return_pct,
        net_pnl=r.net_pnl,
        mae_pct=r.mae_pct,
        mfe_pct=r.mfe_pct,
        r_multiple=r.realized_r_multiple,
        thesis_status=r.thesis_status,
        exit_reason=r.exit_reason,
        resultado=classify_outcome(r.net_pnl),
    )


def _cumple(f: OperacionFila, filtro: FiltroOperaciones) -> bool:
    if filtro.resultado is not None and f.resultado != filtro.resultado:
        return False
    if filtro.exit_reason is not None and f.exit_reason != filtro.exit_reason:
        return False
    if filtro.thesis_status is not None and f.thesis_status != filtro.thesis_status:
        return False
    if filtro.score_min is not None and (f.score is None or f.score < filtro.score_min):
        return False
    if filtro.score_max is not None and (f.score is None or f.score > filtro.score_max):
        return False
    return True


def list_operaciones(session: Session, filtro: FiltroOperaciones | None = None) -> tuple[OperacionFila, ...]:
    """Operaciones cerradas que cumplen el filtro, la más reciente primero."""
    filtro = filtro or FiltroOperaciones()
    filas = [
        _fila(to_domain(r))
        for r in list_trade_results(
            session, exit_desde=filtro.exit_desde, exit_hasta=filtro.exit_hasta, symbol=filtro.symbol
        )
    ]
    return tuple(sorted((f for f in filas if _cumple(f, filtro)), key=lambda f: (f.exit_time, str(f.position_id)), reverse=True))


def _trayectoria(session: Session, position_id: uuid.UUID, max_puntos: int) -> tuple[PuntoTrayectoria, ...]:
    """Todos los snapshots de evento (entrada, TP, trailing, salida...) y los
    de rutina (`SCHEDULED`) adelgazados para poder dibujarlos."""
    snaps = list_snapshots_for_position(session, position_id)
    eventos = [s for s in snaps if s.system_state.value != "SCHEDULED"]
    rutina = [s for s in snaps if s.system_state.value == "SCHEDULED"]
    cupo = max(max_puntos - len(eventos), 2)
    if len(rutina) > cupo:
        paso = math.ceil(len(rutina) / cupo)
        rutina = rutina[::paso] + ([rutina[-1]] if (len(rutina) - 1) % paso else [])
    elegidos = sorted(eventos + rutina, key=lambda s: (s.snapshot_time, s.id))
    return tuple(
        PuntoTrayectoria(
            time=s.snapshot_time,
            market_price=s.market_price,
            unrealized_pnl=s.unrealized_pnl,
            unrealized_return_pct=s.unrealized_return_pct,
            stop_price=s.stop_price,
            target_price=s.target_price,
            trailing_active=s.trailing_active,
            system_state=s.system_state.value,
        )
        for s in elegidos
    )


def _decision(session: Session, proposal_id: uuid.UUID | None) -> DecisionView | None:
    if proposal_id is None:
        return None
    p = get_propuesta(session, proposal_id)
    if p is None:
        return None
    return DecisionView(
        proposal_id=p.id,
        cycle_id=p.ciclo_id,
        categoria=p.categoria,
        horizonte=p.horizonte,
        nivel_riesgo=p.nivel_riesgo,
        puntaje_base=p.puntaje_base,
        calidad_datos=p.calidad_datos,
        puntaje_ponderado=p.puntaje_ponderado,
        confianza_global=p.confianza_global,
        tesis=p.tesis,
        invalidacion=p.invalidacion,
        escenario_adverso=p.escenario_adverso,
        comentario_chartista=p.comentario_chartista,
        fuentes=tuple(p.fuentes),
        decision_snapshot=p.decision_snapshot,
        factores=tuple(
            FactorView(f.factor, f.peso, f.puntaje, f.confianza) for f in get_factores_de_propuesta(session, p.id)
        ),
    )


def get_operacion_detalle(
    session: Session, position_id: uuid.UUID, *, max_puntos: int = MAX_PUNTOS_TRAYECTORIA
) -> OperacionDetalle | None:
    row = get_trade_result_by_position(session, position_id)
    if row is None:
        return None
    resultado = to_domain(row)
    return OperacionDetalle(
        fila=_fila(resultado),
        resultado=resultado,
        decision=_decision(session, resultado.proposal_id),
        trayectoria=_trayectoria(session, position_id, max_puntos),
    )
