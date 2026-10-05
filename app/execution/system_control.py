"""Control manual del sistema (Paso 8): estado y reanudación tras una pausa
por drawdown (decisiones del 2026-10-04/05).

La pausa la registra el monitor solo; **reanudar es siempre manual**. Al
reanudar, el pico de equity se REINICIA al equity de ese momento (si no, la
regla `equity <= pico × 85%` volvería a pausar al instante) y el pico anterior
queda guardado como registro. Eso se escribe en dos tablas en la misma
transacción:

- `eventos_sistema`: el evento `REANUDACION` (cambia el estado a "no pausado");
- `equity_observaciones`: una fila con origen `REANUDACION` y `pico = equity`
  (así el pico vigente, que siempre se lee de la última observación, ya es el
  nuevo).

Para reanudar se necesita el equity de AHORA, o sea el precio de todas las
posiciones abiertas; si falta alguno no se reanuda (no se inventa un precio).
El candado del equity serializa esta operación con el monitor.

No importa nada de IA (P2). La transacción la maneja quien llama.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

from sqlalchemy.orm import Session

from app.domain.enums import OrigenObservacionEquity, TipoEventoSistema
from app.domain.rules.risk import sistema_pausado
from app.execution.emergency_stop import EmergencyStop, FileEmergencyStop
from app.execution.monitor import current_equity
from app.execution.price_source import PriceSource, PriceUnavailable
from app.repositories.equity import (
    EquityObservacionRow,
    acquire_equity_lock,
    append_observacion,
    get_ultima_observacion,
)
from app.repositories.positions import list_open_posiciones
from app.repositories.system_events import EventoSistemaRow, append_evento_sistema, get_ultimo_evento_sistema

logger = logging.getLogger(__name__)

_COMPONENT = "execution.system_control"


class ResultadoReanudacion(str, Enum):
    REANUDADO = "REANUDADO"
    NO_PAUSADO = "NO_PAUSADO"
    SIN_PRECIO = "SIN_PRECIO"


@dataclass(frozen=True)
class EstadoDelSistema:
    pausado: bool
    parada_emergencia: bool
    posiciones_abiertas: int
    ultimo_equity: Decimal | None
    pico_vigente: Decimal
    ultima_observacion_en: datetime | None


def _exigir_utc(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("`now` debe ser un datetime con zona horaria UTC explícita (sección 15.3 del plan).")


def get_system_state(
    session: Session, *, initial_capital: Decimal, emergency_stop: EmergencyStop | None = None
) -> EstadoDelSistema:
    """Foto del estado, solo lectura: lo último que guardó el monitor."""
    ultimo_evento = get_ultimo_evento_sistema(session)
    ultima = get_ultima_observacion(session)
    return EstadoDelSistema(
        pausado=sistema_pausado(ultimo_evento.event_type if ultimo_evento else None),
        parada_emergencia=(emergency_stop or FileEmergencyStop()).is_active(),
        posiciones_abiertas=len(list_open_posiciones(session)),
        ultimo_equity=ultima.equity_usd if ultima else None,
        pico_vigente=ultima.peak_equity_usd if ultima else initial_capital,
        ultima_observacion_en=ultima.observed_at if ultima else None,
    )


def resume_system(
    session: Session, *, now: datetime, price_source: PriceSource, initial_capital: Decimal
) -> tuple[ResultadoReanudacion, Decimal | None]:
    """Reanuda tras una pausa por drawdown. Devuelve el resultado y el equity
    de ese momento (o `None` si no se pudo calcular)."""
    _exigir_utc(now)
    acquire_equity_lock(session)

    ultimo_evento = get_ultimo_evento_sistema(session)
    if not sistema_pausado(ultimo_evento.event_type if ultimo_evento else None):
        return ResultadoReanudacion.NO_PAUSADO, None

    precios = {}
    for posicion in list_open_posiciones(session):
        try:
            precios[posicion.symbol] = price_source.get_price(posicion.symbol)
        except PriceUnavailable:
            return ResultadoReanudacion.SIN_PRECIO, None

    valoracion = current_equity(session, initial_capital=initial_capital, precios=precios)
    if valoracion is None:  # pragma: no cover - todas las abiertas tienen precio si llegó hasta aquí
        return ResultadoReanudacion.SIN_PRECIO, None
    equity, n_abiertas = valoracion

    ultima = get_ultima_observacion(session)
    pico_anterior = ultima.peak_equity_usd if ultima is not None else initial_capital

    escrito = append_evento_sistema(
        session,
        EventoSistemaRow(
            event_type=TipoEventoSistema.REANUDACION,
            occurred_at=now,
            equity_usd=equity,
            peak_equity_usd=equity,
            previous_peak_equity_usd=pico_anterior,
            detail="Reanudación manual: el pico de equity se reinicia al equity actual.",
        ),
    )
    if not escrito:  # pragma: no cover - descartado arriba bajo el mismo candado
        return ResultadoReanudacion.NO_PAUSADO, None

    append_observacion(
        session,
        EquityObservacionRow(
            observed_at=now,
            origen=OrigenObservacionEquity.REANUDACION,
            equity_usd=equity,
            peak_equity_usd=equity,
            open_positions=n_abiertas,
        ),
    )
    logger.warning(
        "system-control: sistema REANUDADO",
        extra={
            "component": _COMPONENT,
            "equity": str(equity),
            "previous_peak_equity": str(pico_anterior),
        },
    )
    return ResultadoReanudacion.REANUDADO, equity
