"""Monitor determinista de posiciones abiertas (Paso 8; sección 9 del plan).

"El monitoreo de posiciones abiertas corre en Python puro, cada 60 segundos,
sin ninguna llamada a un modelo." Este módulo es UN chequeo (`run_monitor_tick`);
quien lo repite cada 60 s es `scripts/monitor.py` (y más adelante APScheduler).
No importa nada de IA (P2) y no calcula ninguna regla: las reglas están en
`app.domain.rules.risk` y los cierres en `app.execution.position_manager`.

Un chequeo hace, en este orden y en UNA transacción (el commit lo hace quien
llama):

1. toma el candado del equity (un solo monitor/reanudación a la vez);
2. reconcilia el estado (`app.execution.reconciliation`); si algo no cuadra
   lanza `ReconciliationBlocked` y NO toca nada: el plan manda "bloquear
   operación hasta resolver";
3. expira las entradas pendientes vencidas (devuelve sus cupos);
4. para cada posición abierta pide el precio (ticker público de Binance) y
   aplica las reglas: stop, TP parcial, trailing. Cada posición va en su
   propio savepoint: un error en una no deshace las demás;
5. si TODAS las posiciones abiertas tuvieron precio y ninguna falló, calcula
   el equity, guarda una observación con el pico vigente y, si el drawdown
   llegó a -15%, registra la pausa. Si falta un precio o hubo un error NO se
   escribe equity: nunca se inventa un precio para rellenar un hueco (P3).

Arranque (decisión del 2026-10-05): el primer chequeo tras un reinicio es
un chequeo más. Revisa las posiciones abiertas con el precio ACTUAL y aplica
las reglas de salida de inmediato; nunca se cierra nada "por reinicio".

Pausa por drawdown: solo bloquea entradas nuevas. El monitor sigue vigilando
y ejecutando stops mientras el sistema está pausado o con `PARAR`.

Fuera de este bloque (Paso 11): las notificaciones de escalamiento (caída
>5%/h, volumen >3σ, Binance caído >5 min) necesitan Telegram.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.enums import EstadoOrden, OrigenObservacionEquity, TipoEventoSistema
from app.domain.rules.risk import (
    OrdenEjecutada,
    PosicionMarcada,
    calculate_drawdown_pct,
    calculate_equity,
    is_drawdown_limit_reached,
    sistema_pausado,
    update_peak_equity,
)
from app.execution.order_manager import expire_pending_entries
from app.execution.position_manager import ResultadoChequeo, check_position
from app.execution.price_source import PriceSource, PriceUnavailable
from app.execution.reconciliation import Discrepancia, reconcile
from app.repositories.equity import (
    EquityObservacionRow,
    acquire_equity_lock,
    append_observacion,
    get_ultima_observacion,
)
from app.repositories.orders import list_orders
from app.repositories.positions import list_open_posiciones
from app.repositories.system_events import EventoSistemaRow, append_evento_sistema, get_ultimo_evento_sistema

logger = logging.getLogger(__name__)

_COMPONENT = "execution.monitor"


class ReconciliationBlocked(Exception):
    """El estado no cuadra: el monitor se detiene sin actuar (ver el docstring
    del módulo). Lleva las discrepancias para mostrarlas."""

    def __init__(self, discrepancias: list[Discrepancia]) -> None:
        self.discrepancias = discrepancias
        super().__init__(f"{len(discrepancias)} discrepancia(s) en la reconciliación")


@dataclass(frozen=True)
class ErrorDePosicion:
    symbol: str
    position_id: uuid.UUID
    mensaje: str


@dataclass(frozen=True)
class ResultadoTick:
    chequeos: tuple[ResultadoChequeo, ...] = ()
    sin_precio: tuple[str, ...] = ()
    errores: tuple[ErrorDePosicion, ...] = ()
    expiradas: int = 0
    equity: Decimal | None = None
    peak_equity: Decimal | None = None
    drawdown_pct: Decimal | None = None
    pausa_registrada: bool = False
    posiciones_abiertas: int = 0
    notas: tuple[str, ...] = field(default=())

    @property
    def equity_calculada(self) -> bool:
        return self.equity is not None

    @property
    def hubo_problemas(self) -> bool:
        return bool(self.sin_precio or self.errores)


def _exigir_utc(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("`now` debe ser un datetime con zona horaria UTC explícita (sección 15.3 del plan).")


def current_equity(
    session: Session, *, initial_capital: Decimal, precios: dict[str, Decimal]
) -> tuple[Decimal, int] | None:
    """Equity actual y cuántas posiciones abiertas se valoraron, o `None` si
    falta el precio de alguna posición abierta (no se inventa)."""
    abiertas = list_open_posiciones(session)
    if any(p.symbol not in precios for p in abiertas):
        return None
    ejecutadas = [
        OrdenEjecutada(side=o.side, quantity=o.quantity, execution_price=o.execution_price, fee=o.fee)
        for o in list_orders(session, estados=[EstadoOrden.FILLED])
    ]
    equity = calculate_equity(
        initial_capital=initial_capital,
        filled_orders=ejecutadas,
        open_positions=[PosicionMarcada(p.remaining_quantity, precios[p.symbol]) for p in abiertas],
    )
    return equity, len(abiertas)


def run_monitor_tick(
    session: Session, *, now: datetime, price_source: PriceSource, initial_capital: Decimal
) -> ResultadoTick:
    """Un chequeo completo (ver el docstring del módulo)."""
    _exigir_utc(now)
    acquire_equity_lock(session)

    discrepancias = reconcile(session)
    if discrepancias:
        for d in discrepancias:
            _log("discrepancia de reconciliación", level=logging.ERROR, codigo=d.codigo.value, detalle=d.detalle)
        raise ReconciliationBlocked(discrepancias)

    expiradas = expire_pending_entries(session, now)

    chequeos: list[ResultadoChequeo] = []
    sin_precio: list[str] = []
    errores: list[ErrorDePosicion] = []
    precios: dict[str, Decimal] = {}

    for posicion in list_open_posiciones(session):
        try:
            precio = price_source.get_price(posicion.symbol)
        except PriceUnavailable:
            sin_precio.append(posicion.symbol)
            _log("sin precio válido", level=logging.WARNING, symbol=posicion.symbol)
            continue
        precios[posicion.symbol] = precio
        try:
            with session.begin_nested():
                chequeos.append(check_position(session, posicion, precio, now=now))
        except Exception as exc:  # noqa: BLE001 - se registra y el resto de posiciones sigue
            errores.append(ErrorDePosicion(posicion.symbol, posicion.id, f"{type(exc).__name__}: {exc}"))
            logger.error(
                "monitor: error al chequear una posición",
                exc_info=True,
                extra={"component": _COMPONENT, "symbol": posicion.symbol, "position_id": str(posicion.id)},
            )

    base = dict(
        chequeos=tuple(chequeos),
        sin_precio=tuple(sin_precio),
        errores=tuple(errores),
        expiradas=expiradas,
    )

    if sin_precio or errores:
        return ResultadoTick(
            **base,
            posiciones_abiertas=len(list_open_posiciones(session)),
            notas=("Equity no calculada en este chequeo: falta un precio o hubo un error.",),
        )

    valoracion = current_equity(session, initial_capital=initial_capital, precios=precios)
    if valoracion is None:  # pragma: no cover - todas las abiertas tienen precio si no hubo problemas
        raise RuntimeError("Falta el precio de una posición abierta pese a no haber reportado problemas")
    equity, n_abiertas = valoracion

    ultima = get_ultima_observacion(session)
    pico_previo = ultima.peak_equity_usd if ultima is not None else initial_capital
    peak = update_peak_equity(pico_previo, equity)
    append_observacion(
        session,
        EquityObservacionRow(
            observed_at=now,
            origen=OrigenObservacionEquity.MONITOR,
            equity_usd=equity,
            peak_equity_usd=peak,
            open_positions=n_abiertas,
        ),
    )

    pausa_registrada = False
    ultimo_evento = get_ultimo_evento_sistema(session)
    ya_pausado = sistema_pausado(ultimo_evento.event_type if ultimo_evento else None)
    if not ya_pausado and is_drawdown_limit_reached(equity, peak):
        drawdown = calculate_drawdown_pct(equity, peak)
        pausa_registrada = append_evento_sistema(
            session,
            EventoSistemaRow(
                event_type=TipoEventoSistema.PAUSA_DRAWDOWN,
                occurred_at=now,
                equity_usd=equity,
                peak_equity_usd=peak,
                detail=f"Drawdown {drawdown}% desde el pico de equity (límite -15%).",
            ),
        )
        if pausa_registrada:
            _log(
                "PAUSA por drawdown",
                level=logging.WARNING,
                equity=str(equity),
                peak_equity=str(peak),
                drawdown_pct=str(drawdown),
            )

    return ResultadoTick(
        **base,
        equity=equity,
        peak_equity=peak,
        drawdown_pct=calculate_drawdown_pct(equity, peak),
        pausa_registrada=pausa_registrada,
        posiciones_abiertas=n_abiertas,
    )


def _log(message: str, *, level: int = logging.INFO, **context) -> None:
    logger.log(
        level,
        f"monitor: {message}",
        extra={"component": _COMPONENT, **{k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in context.items()}},
    )
