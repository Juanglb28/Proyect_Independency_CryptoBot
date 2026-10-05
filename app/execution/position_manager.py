"""Cierres de posiciones del motor de ejecución paper (Paso 8).

Aplica a UNA posición abierta lo que dicen las reglas puras de
`app.domain.rules.risk.evaluate_position` ante un precio observado: no
decide nada por sí mismo (P1) y no importa nada de IA (P2).

Qué hace `check_position`:

- `MANTENER`: si el trailing está activo y el precio máximo observado subió,
  lo persiste (el máximo solo sube); no crea órdenes.
- `CERRAR_PARCIAL` (+12%): vende el 50% al precio observado, deja el resto
  `OPEN` con el trailing activado y su primer máximo.
- `CERRAR_TOTAL` (stop -8% o trailing 6%): vende todo lo que queda al precio
  observado y cierra la posición.

Garantías (P5):

- **Atómico**: la orden de salida `FILLED` y el cambio de la posición se
  escriben en la MISMA transacción (el commit lo hace quien llama). Nunca
  queda una venta sin su posición actualizada, ni al revés.
- **Idempotente**: el `client_order_id` de una salida es determinista
  (`exit-<position_id>-<intención>`) y la base lo exige único. Reintentar un
  chequeo no puede vender dos veces: tras el cierre parcial la posición ya
  tiene `take_profit_hit`, y tras un cierre total ya no está `OPEN`. Si una
  orden con ese identificador ya existe pero la posición todavía cumple la
  condición, el estado es incoherente: se lanza `RuntimeError` para revertir
  todo en vez de "arreglarlo" en silencio.
- **Sin precio no hay orden**: este módulo recibe el precio ya obtenido; quién
  lo consigue (y qué pasa si no hay) es del monitor.
- Los campos de la decisión original (`entry_price`, niveles iniciales,
  `decision_snapshot` de la propuesta) no se tocan nunca.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.models.positions import Posicion
from app.domain.enums import EstadoOrden, EstadoPosicion, IntencionOrden, LadoOrden
from app.domain.rules.risk import AccionMonitor, EstadoParaMonitor, calculate_fee, evaluate_position
from app.repositories.orders import OrdenRow, create_orden
from app.repositories.positions import update_open_position

logger = logging.getLogger(__name__)

_COMPONENT = "execution.position_manager"


@dataclass(frozen=True)
class ResultadoChequeo:
    posicion_id: uuid.UUID
    symbol: str
    accion: AccionMonitor
    observed_price: Decimal
    intencion: IntencionOrden | None = None
    orden_id: uuid.UUID | None = None
    trailing_peak_price: Decimal | None = None
    trailing_stop_price: Decimal | None = None


def exit_client_order_id(position_id: uuid.UUID, intencion: IntencionOrden) -> str:
    """Identificador de idempotencia de una orden de salida. Una posición tiene
    a lo sumo una salida por intención (un TP parcial, y un cierre por stop o
    por trailing, que cierran la posición)."""
    return f"exit-{position_id}-{intencion.value}"


def _exigir_utc(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("`now` debe ser un datetime con zona horaria UTC explícita (sección 15.3 del plan).")


def check_position(
    session: Session, posicion: Posicion, observed_price: Decimal, *, now: datetime
) -> ResultadoChequeo:
    """Evalúa una posición `OPEN` contra el precio observado y aplica el
    resultado (ver el docstring del módulo)."""
    _exigir_utc(now)
    if posicion.status != EstadoPosicion.OPEN:
        raise ValueError(f"La posición {posicion.id} no está abierta ({posicion.status.value})")

    decision = evaluate_position(
        EstadoParaMonitor(
            remaining_quantity=posicion.remaining_quantity,
            initial_stop_price=posicion.initial_stop_price,
            initial_target_price=posicion.initial_target_price,
            take_profit_hit=posicion.take_profit_hit,
            trailing_active=posicion.trailing_active,
            trailing_peak_price=posicion.trailing_peak_price,
        ),
        observed_price,
    )

    if decision.accion == AccionMonitor.MANTENER:
        if decision.trailing_active and decision.trailing_peak_price != posicion.trailing_peak_price:
            _aplicar(
                session,
                posicion,
                status=EstadoPosicion.OPEN,
                remaining=posicion.remaining_quantity,
                decision=decision,
                closed_at=None,
            )
        return ResultadoChequeo(
            posicion_id=posicion.id,
            symbol=posicion.symbol,
            accion=decision.accion,
            observed_price=observed_price,
            trailing_peak_price=decision.trailing_peak_price,
            trailing_stop_price=decision.trailing_stop_price,
        )

    # Hay una salida: se crea primero la orden y luego se actualiza la posición,
    # ambas en esta transacción.
    cantidad = decision.cantidad_a_vender
    fee = calculate_fee(cantidad, observed_price)
    orden_id, creada = create_orden(
        session,
        OrdenRow(
            client_order_id=exit_client_order_id(posicion.id, decision.intencion),
            proposal_id=posicion.proposal_id,
            symbol=posicion.symbol,
            side=LadoOrden.SELL,
            intencion=decision.intencion,
            estado=EstadoOrden.FILLED,
            position_id=posicion.id,
            quantity=cantidad,
            reference_price=observed_price,
            execution_price=observed_price,
            fee=fee,
            filled_at=now,
            created_at=now,
        ),
    )
    if not creada:
        raise RuntimeError(
            f"Ya existe la orden {exit_client_order_id(posicion.id, decision.intencion)!r} pero la posición "
            f"{posicion.id} sigue cumpliendo la condición de salida: estado incoherente"
        )

    restante = posicion.remaining_quantity - cantidad
    if decision.accion == AccionMonitor.CERRAR_TOTAL:
        _aplicar(
            session,
            posicion,
            status=EstadoPosicion.CLOSED,
            remaining=Decimal(0),
            decision=decision,
            closed_at=now,
        )
    else:
        _aplicar(
            session, posicion, status=EstadoPosicion.OPEN, remaining=restante, decision=decision, closed_at=None
        )

    _log(
        "salida ejecutada",
        position_id=posicion.id,
        symbol=posicion.symbol,
        intencion=decision.intencion.value,
        quantity=str(cantidad),
        observed_price=str(observed_price),
    )
    return ResultadoChequeo(
        posicion_id=posicion.id,
        symbol=posicion.symbol,
        accion=decision.accion,
        observed_price=observed_price,
        intencion=decision.intencion,
        orden_id=orden_id,
        trailing_peak_price=decision.trailing_peak_price,
        trailing_stop_price=decision.trailing_stop_price,
    )


def _aplicar(session: Session, posicion: Posicion, *, status, remaining, decision, closed_at) -> None:
    actualizada = update_open_position(
        session,
        posicion.id,
        status=status,
        remaining_quantity=remaining,
        take_profit_hit=decision.take_profit_hit,
        trailing_active=decision.trailing_active,
        trailing_peak_price=decision.trailing_peak_price,
        closed_at=closed_at,
    )
    if not actualizada:
        # Otro proceso la cerró entre la lectura y el UPDATE: se aborta para
        # que se revierta también la orden recién creada.
        raise RuntimeError(f"La posición {posicion.id} dejó de estar abierta durante el chequeo")
    # El objeto ORM en memoria refleja lo que quedó en la base.
    session.refresh(posicion)


def _log(message: str, **context) -> None:
    logger.info(
        f"position-manager: {message}",
        extra={"component": _COMPONENT, **{k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in context.items()}},
    )
