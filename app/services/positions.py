"""Posiciones: estado operativo actual (sección 8.9). Sin llamadas a IA.

Todo lo numérico (P&L no realizado, distancias a stop y objetivo, MAE/MFE
parciales) viene del ÚLTIMO snapshot que guardó el monitor
(`trade_snapshots`); aquí no se recalcula. Si una posición aún no tiene
snapshot, esos campos quedan `None` (no se inventan).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.enums import EstadoOrden, IntencionOrden, SerieOperacion
from app.domain.rules.risk import MAX_POSICIONES_ABIERTAS, calculate_position_value
from app.execution.emergency_stop import EmergencyStop
from app.execution.system_control import get_system_state
from app.repositories.orders import list_orders, list_orders_for_position
from app.repositories.positions import list_open_posiciones
from app.repositories.snapshots import get_ultimo_snapshot


@dataclass(frozen=True)
class PosicionAbiertaView:
    position_id: uuid.UUID
    proposal_id: uuid.UUID
    symbol: str
    opened_at: datetime
    entry_price: Decimal
    initial_quantity: Decimal
    remaining_quantity: Decimal
    initial_stop_price: Decimal
    initial_target_price: Decimal
    take_profit_hit: bool
    trailing_active: bool
    ultimo_precio: Decimal | None
    ultima_observacion_en: datetime | None
    pnl_no_realizado: Decimal | None
    retorno_no_realizado_pct: Decimal | None
    stop_vigente: Decimal | None
    objetivo_vigente: Decimal | None
    distancia_stop_pct: Decimal | None
    distancia_objetivo_pct: Decimal | None
    mae_pct: Decimal | None
    mfe_pct: Decimal | None
    unidad: str = ""
    """Activo base de la posición (BTC en BTCUSDT): lo que se compró, para rotular las unidades."""
    valor_de_compra_usd: Decimal | None = None
    """Precio total de la compra: unidades compradas × precio de ejecución (la orden de
    entrada ejecutada), SIN comisión. `None` si no hay orden de entrada ejecutada."""
    comision_entrada_usd: Decimal | None = None
    serie: SerieOperacion = SerieOperacion.ESTRATEGIA
    """Estrategia u observación (decisión del 2026-10-12); la observación no usa los cupos de la estrategia."""


@dataclass(frozen=True)
class EntradaPendienteView:
    order_id: uuid.UUID
    proposal_id: uuid.UUID
    symbol: str
    requested_usd: Decimal | None
    reference_price: Decimal | None
    created_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True)
class PosicionesView:
    generado_en: datetime
    posiciones: tuple[PosicionAbiertaView, ...]
    entradas_pendientes: tuple[EntradaPendienteView, ...]
    maximo_posiciones: int
    cupos_libres: int
    sistema_pausado: bool
    parada_emergencia: bool
    ultima_observacion_en: datetime | None


_QUOTE = "USDT"


def _unidad(symbol: str) -> str:
    return symbol[: -len(_QUOTE)] if symbol.endswith(_QUOTE) and len(symbol) > len(_QUOTE) else symbol


def _compra(session: Session, position_id: uuid.UUID) -> tuple[Decimal | None, Decimal | None]:
    """(valor de compra, comisión) de la orden de ENTRADA ejecutada de la posición."""
    entrada = next(
        (
            o
            for o in list_orders_for_position(session, position_id)
            if o.intencion == IntencionOrden.ENTRY and o.estado == EstadoOrden.FILLED
        ),
        None,
    )
    if entrada is None or entrada.quantity is None or entrada.execution_price is None:
        return None, None
    return calculate_position_value(entrada.quantity, entrada.execution_price), entrada.fee


def get_positions_view(
    session: Session, *, initial_capital: Decimal, now: datetime, emergency_stop: EmergencyStop | None = None
) -> PosicionesView:
    estado = get_system_state(session, initial_capital=initial_capital, emergency_stop=emergency_stop)

    filas: list[PosicionAbiertaView] = []
    ids_observacion = {p.id for p in list_open_posiciones(session, serie=SerieOperacion.OBSERVACION)}
    for p in list_open_posiciones(session):
        snap = get_ultimo_snapshot(session, p.id)
        valor_de_compra, comision = _compra(session, p.id)
        filas.append(
            PosicionAbiertaView(
                position_id=p.id,
                proposal_id=p.proposal_id,
                symbol=p.symbol,
                opened_at=p.opened_at,
                entry_price=p.entry_price,
                initial_quantity=p.initial_quantity,
                remaining_quantity=p.remaining_quantity,
                initial_stop_price=p.initial_stop_price,
                initial_target_price=p.initial_target_price,
                take_profit_hit=p.take_profit_hit,
                trailing_active=p.trailing_active,
                ultimo_precio=None if snap is None else snap.market_price,
                ultima_observacion_en=None if snap is None else snap.snapshot_time,
                pnl_no_realizado=None if snap is None else snap.unrealized_pnl,
                retorno_no_realizado_pct=None if snap is None else snap.unrealized_return_pct,
                stop_vigente=None if snap is None else snap.stop_price,
                objetivo_vigente=None if snap is None else snap.target_price,
                distancia_stop_pct=None if snap is None else snap.distance_to_stop_pct,
                distancia_objetivo_pct=None if snap is None else snap.distance_to_target_pct,
                mae_pct=None if snap is None else snap.current_mae_pct,
                mfe_pct=None if snap is None else snap.current_mfe_pct,
                unidad=_unidad(p.symbol),
                valor_de_compra_usd=valor_de_compra,
                comision_entrada_usd=comision,
                serie=SerieOperacion.OBSERVACION if p.id in ids_observacion else SerieOperacion.ESTRATEGIA,
            )
        )

    pendientes = tuple(
        EntradaPendienteView(
            order_id=o.id,
            proposal_id=o.proposal_id,
            symbol=o.symbol,
            requested_usd=o.requested_usd,
            reference_price=o.reference_price,
            created_at=o.created_at,
            expires_at=o.expires_at,
        )
        for o in list_orders(session, estados=[EstadoOrden.PENDING_APPROVAL])
    )
    return PosicionesView(
        generado_en=now,
        posiciones=tuple(filas),
        entradas_pendientes=pendientes,
        maximo_posiciones=MAX_POSICIONES_ABIERTAS,
        cupos_libres=max(
            MAX_POSICIONES_ABIERTAS - sum(1 for x in filas if x.serie == SerieOperacion.ESTRATEGIA) - len(pendientes), 0
        ),
        sistema_pausado=estado.pausado,
        parada_emergencia=estado.parada_emergencia,
        ultima_observacion_en=estado.ultima_observacion_en,
    )
