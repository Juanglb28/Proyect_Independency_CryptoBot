"""Puente entre el motor de ejecución y la capa de resultados (Paso 9).

El monitor y el servicio de entradas deciden y ejecutan; este módulo solo
REGISTRA lo ocurrido: snapshots de la trayectoria (sección 8.5 del plan) y el
`trade_result` al cerrar (sección 8.4). Reglas de oro:

- **Registrar nunca bloquea operar.** Cada escritura va en su propio
  savepoint y un fallo se registra en el log y se traga: una entrada, un stop
  o un trailing JAMÁS dejan de ejecutarse porque falló un snapshot o el
  cálculo de un resultado. Lo que falte se repara después: el monitor corre
  `ensure_trade_results` en cada chequeo y genera el resultado de toda
  posición cerrada que no lo tenga.
- Aquí no hay fórmulas: las de snapshots y resultados están en
  `app.domain.rules.results` (el nivel del trailing, en `risk.py`).
- Snapshots que se emiten (ver `EstadoSnapshot`): `ENTRY` al abrir;
  `SCHEDULED` en cada chequeo de una posición que sigue abierta;
  `TRAILING_UPDATED` cuando el máximo del trailing sube;
  `PARTIAL_TAKE_PROFIT` + `TRAILING_ACTIVATED` al ejecutar el TP parcial;
  `EXIT` al cerrar; `SYSTEM_PAUSED` para cada posición abierta cuando se
  registra una pausa por drawdown.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.enums import EstadoSnapshot
from app.domain.rules.risk import AccionMonitor, calculate_trailing_stop
from app.execution.position_manager import ResultadoChequeo
from app.execution.price_source import PriceSource
from app.execution.reference_prices import fetch_reference_prices
from app.results.snapshot_service import EstadoParaSnapshot, record_snapshot
from app.results.trade_result_service import generate_trade_result

logger = logging.getLogger(__name__)

_COMPONENT = "execution.results_recorder"


@dataclass(frozen=True)
class EstadoPrevio:
    """Foto de la posición ANTES del chequeo (el objeto ORM se refresca
    durante el chequeo y ya no permite ver cómo estaba)."""

    remaining_quantity: Decimal
    trailing_active: bool
    trailing_peak_price: Decimal | None


def capture_state(posicion) -> EstadoPrevio:
    return EstadoPrevio(posicion.remaining_quantity, posicion.trailing_active, posicion.trailing_peak_price)


def _niveles(posicion, *, trailing_active: bool, peak: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    """`(stop, objetivo)` vigentes: con el trailing activo, el stop es su nivel
    de salida y ya no hay objetivo; si no, los niveles iniciales."""
    if trailing_active:
        return calculate_trailing_stop(peak), None
    return posicion.initial_stop_price, posicion.initial_target_price


def _snapshot(
    session: Session,
    posicion,
    *,
    estado: EstadoSnapshot,
    now: datetime,
    price: Decimal,
    quantity: Decimal,
    trailing_active: bool,
    peak: Decimal | None,
) -> None:
    stop, target = _niveles(posicion, trailing_active=trailing_active, peak=peak)
    record_snapshot(
        session,
        estado=EstadoParaSnapshot(
            position_id=posicion.id,
            symbol=posicion.symbol,
            entry_price=posicion.entry_price,
            quantity=quantity,
            trailing_active=trailing_active,
            trailing_reference_price=peak,
        ),
        system_state=estado,
        snapshot_time=now,
        market_price=price,
        stop_price=stop,
        target_price=target,
    )


def record_entry(session: Session, posicion, *, now: datetime) -> None:
    """Snapshot `ENTRY` de una posición recién abierta (nunca bloquea)."""
    try:
        with session.begin_nested():
            _snapshot(
                session,
                posicion,
                estado=EstadoSnapshot.ENTRY,
                now=now,
                price=posicion.entry_price,
                quantity=posicion.initial_quantity,
                trailing_active=False,
                peak=None,
            )
    except Exception:  # noqa: BLE001 - registrar nunca bloquea una entrada
        _log_error("no se pudo registrar el snapshot de entrada", posicion.id)


def record_check(
    session: Session,
    posicion,
    *,
    antes: EstadoPrevio,
    resultado: ResultadoChequeo,
    now: datetime,
    price_source: PriceSource,
    initial_capital: Decimal,
) -> None:
    """Registra lo ocurrido en un chequeo de la posición (snapshots y, si se
    cerró, su `trade_result`). `posicion` ya refleja el estado posterior."""
    precio = resultado.observed_price
    try:
        with session.begin_nested():
            _registrar_snapshots(session, posicion, antes=antes, resultado=resultado, now=now, precio=precio)
    except Exception:  # noqa: BLE001 - registrar nunca bloquea una salida
        _log_error("no se pudieron registrar los snapshots del chequeo", posicion.id)

    if resultado.accion == AccionMonitor.CERRAR_TOTAL:
        try:
            exit_prices = fetch_reference_prices(price_source, known={posicion.symbol: precio})
            with session.begin_nested():
                generate_trade_result(session, posicion.id, initial_capital=initial_capital, exit_prices=exit_prices)
        except Exception:  # noqa: BLE001 - el barrido `ensure_trade_results` lo repara
            _log_error("no se pudo generar el trade_result al cerrar", posicion.id)


def _registrar_snapshots(session, posicion, *, antes: EstadoPrevio, resultado: ResultadoChequeo, now, precio) -> None:
    if resultado.accion == AccionMonitor.CERRAR_TOTAL:
        _snapshot(
            session, posicion, estado=EstadoSnapshot.EXIT, now=now, price=precio,
            quantity=antes.remaining_quantity, trailing_active=antes.trailing_active,
            peak=resultado.trailing_peak_price if antes.trailing_active else None,
        )
        return

    if resultado.accion == AccionMonitor.CERRAR_PARCIAL:
        _snapshot(
            session, posicion, estado=EstadoSnapshot.PARTIAL_TAKE_PROFIT, now=now, price=precio,
            quantity=antes.remaining_quantity, trailing_active=False, peak=None,
        )
        _snapshot(
            session, posicion, estado=EstadoSnapshot.TRAILING_ACTIVATED, now=now, price=precio,
            quantity=posicion.remaining_quantity, trailing_active=True, peak=posicion.trailing_peak_price,
        )
    elif antes.trailing_active and posicion.trailing_peak_price != antes.trailing_peak_price:
        _snapshot(
            session, posicion, estado=EstadoSnapshot.TRAILING_UPDATED, now=now, price=precio,
            quantity=posicion.remaining_quantity, trailing_active=True, peak=posicion.trailing_peak_price,
        )

    _snapshot(
        session, posicion, estado=EstadoSnapshot.SCHEDULED, now=now, price=precio,
        quantity=posicion.remaining_quantity, trailing_active=posicion.trailing_active,
        peak=posicion.trailing_peak_price,
    )


def record_pause(session: Session, posiciones, precios: dict[str, Decimal], *, now: datetime) -> None:
    """Snapshot `SYSTEM_PAUSED` de cada posición abierta con precio conocido."""
    for posicion in posiciones:
        if posicion.symbol not in precios:
            continue
        try:
            with session.begin_nested():
                _snapshot(
                    session, posicion, estado=EstadoSnapshot.SYSTEM_PAUSED, now=now,
                    price=precios[posicion.symbol], quantity=posicion.remaining_quantity,
                    trailing_active=posicion.trailing_active, peak=posicion.trailing_peak_price,
                )
        except Exception:  # noqa: BLE001
            _log_error("no se pudo registrar el snapshot de pausa", posicion.id)


def record_risk_alert(session: Session, posicion, *, price: Decimal, now: datetime) -> None:
    """Snapshot `RISK_ALERT` de una posición abierta cuando se le envió un
    aviso de escalamiento (caída o volumen). Nunca bloquea al monitor."""
    try:
        with session.begin_nested():
            _snapshot(
                session, posicion, estado=EstadoSnapshot.RISK_ALERT, now=now, price=price,
                quantity=posicion.remaining_quantity, trailing_active=posicion.trailing_active,
                peak=posicion.trailing_peak_price,
            )
    except Exception:  # noqa: BLE001
        _log_error("no se pudo registrar el snapshot de alerta de riesgo", posicion.id)


def _log_error(message: str, position_id: uuid.UUID) -> None:
    logger.error(
        f"results-recorder: {message}",
        exc_info=True,
        extra={"component": _COMPONENT, "position_id": str(position_id)},
    )
