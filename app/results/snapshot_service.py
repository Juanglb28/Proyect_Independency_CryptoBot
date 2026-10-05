"""Servicio de `trade_snapshots` (Paso 9; sección 8.5 del plan): arma y
guarda un snapshot de la trayectoria de una posición abierta.

Las fórmulas (distancias, P&L no realizado, MAE/MFE corrientes) viven en
`app.domain.rules.results.calculate_snapshot_metrics`; aquí solo se juntan los
datos y se persiste (idempotente, ver `app.repositories.snapshots`). No
importa nada de ejecución ni de IA: quien llama (el monitor o el servicio de
entradas) le pasa el estado ya conocido de la posición y el precio observado.

Indicadores del snapshot (`atr`, `relative_volume`, `rsi_1h`, `rsi_4h`): se
leen de `indicadores` SIN look-ahead: solo velas ya CERRADAS en el instante
del snapshot (`calculated_at + duración de la vela <= snapshot_time`). Como
los indicadores se recalculan una vez al día, el valor puede tener horas de
antigüedad; es lo que el sistema SABÍA en ese instante. `atr` y
`relative_volume` usan la temporalidad de 1h (la más fina; el plan no dice
cuál, derivado por mí, el usuario puede corregirlo). Sin dato → `NULL`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.data.indicator_calculator import (
    INDICATOR_NAME_ATR,
    INDICATOR_NAME_RSI,
    INDICATOR_NAME_VOLUMEN_RELATIVO,
)
from app.domain.enums import EstadoSnapshot
from app.domain.rules.results import calculate_snapshot_metrics
from app.repositories.indicators import get_latest_indicator_value
from app.repositories.snapshots import append_snapshot, get_price_extremes

logger = logging.getLogger(__name__)

_DURACION_VELA = {"1h": timedelta(hours=1), "4h": timedelta(hours=4)}


@dataclass(frozen=True)
class EstadoParaSnapshot:
    """Lo que el snapshot necesita saber de la posición en ESE instante."""

    position_id: uuid.UUID
    symbol: str
    entry_price: Decimal
    quantity: Decimal
    """Cantidad a registrar: la restante, o la que estaba en juego antes de
    una venta (`PARTIAL_TAKE_PROFIT` y `EXIT`)."""
    trailing_active: bool
    trailing_reference_price: Decimal | None


def _indicador(session: Session, symbol: str, timeframe: str, nombre: str, at: datetime) -> Decimal | None:
    return get_latest_indicator_value(
        session,
        symbol=symbol,
        timeframe=timeframe,
        indicator_name=nombre,
        calculated_at_or_before=at - _DURACION_VELA[timeframe],
    )


def record_snapshot(
    session: Session,
    *,
    estado: EstadoParaSnapshot,
    system_state: EstadoSnapshot,
    snapshot_time: datetime,
    market_price: Decimal,
    stop_price: Decimal | None,
    target_price: Decimal | None,
) -> bool:
    """Guarda un snapshot. `True` si lo creó, `False` si ya existía uno con la
    misma clave `(posición, instante, estado)` (reintento; no se toca)."""
    if snapshot_time.tzinfo is None or snapshot_time.utcoffset() != timedelta(0):
        raise ValueError("`snapshot_time` debe ser un datetime con zona horaria UTC explícita (sección 15.3).")

    extremos = get_price_extremes(session, estado.position_id, until=snapshot_time)
    m = calculate_snapshot_metrics(
        entry_price=estado.entry_price,
        market_price=market_price,
        quantity=estado.quantity,
        stop_price=stop_price,
        target_price=target_price,
        min_price_seen=None if extremos is None else extremos[0],
        max_price_seen=None if extremos is None else extremos[1],
    )
    _, creado = append_snapshot(
        session,
        {
            "position_id": estado.position_id,
            "symbol": estado.symbol,
            "snapshot_time": snapshot_time,
            "market_price": market_price,
            "market_volume": None,  # la fuente de precios del monitor solo entrega precio
            "quantity": estado.quantity,
            "position_value": m.position_value,
            "unrealized_pnl": m.unrealized_pnl,
            "unrealized_return_pct": m.unrealized_return_pct,
            "stop_price": stop_price,
            "target_price": target_price,
            "distance_to_stop_pct": m.distance_to_stop_pct,
            "distance_to_target_pct": m.distance_to_target_pct,
            "trailing_active": estado.trailing_active,
            "trailing_reference_price": estado.trailing_reference_price,
            "current_mae_pct": m.mae_pct,
            "current_mfe_pct": m.mfe_pct,
            "atr": _indicador(session, estado.symbol, "1h", INDICATOR_NAME_ATR, snapshot_time),
            "relative_volume": _indicador(
                session, estado.symbol, "1h", INDICATOR_NAME_VOLUMEN_RELATIVO, snapshot_time
            ),
            "rsi_1h": _indicador(session, estado.symbol, "1h", INDICATOR_NAME_RSI, snapshot_time),
            "rsi_4h": _indicador(session, estado.symbol, "4h", INDICATOR_NAME_RSI, snapshot_time),
            "system_state": system_state,
        },
    )
    return creado
