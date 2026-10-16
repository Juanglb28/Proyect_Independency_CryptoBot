"""Servicio de `trade_results` (Paso 9; sección 8.4 del plan).

Junta los hechos de una posición CERRADA (órdenes ejecutadas, trayectoria,
decisión de entrada) y se los entrega a la función pura
`app.domain.rules.results.calculate_trade_result`. No calcula nada por sí
mismo (P1) y no importa ejecución ni IA.

Garantías:

- **Un solo resultado por posición** (sección 15.7): la restricción única de
  `trade_results.position_id` hace idempotente el reintento; si ya existe, se
  devuelve el existente sin tocarlo.
- **Nunca se escribe sobre una posición abierta**: `NO_CERRADA`.
- **Sin look-ahead**: el score inicial sale de la propuesta congelada, no de
  datos posteriores. Los benchmarks salen de los precios observados al abrir
  (guardados en la posición) y al cerrar (los pasa el monitor).
- `capital_at_entry`: equity de la última observación con `observed_at <=
  entry_time`; si no hay ninguna, el capital paper inicial (derivado por mí,
  el usuario puede corregirlo; no interviene en ningún cálculo de v1 porque
  el tamaño de posición es fijo).
- Sin precios de salida (p. ej. el resultado se genera después por el
  barrido de reparación) los benchmarks quedan `NULL`: no se inventan.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from sqlalchemy.orm import Session

from app.db.models.trade_results import TradeResultModel
from app.domain.enums import EstadoOrden, EstadoPosicion, IntencionOrden, LadoOrden
from app.domain.rules.results import (
    ContextoDeEntrada,
    OrdenDeTrade,
    PuntoDeTrayectoria,
    TradeResult,
    calculate_trade_benchmark,
    calculate_trade_result,
    parse_prices,
)
from app.domain.symbols import ALLOWED_SYMBOLS
from app.repositories.analysis import get_propuesta
from app.repositories.equity import get_ultima_observacion_hasta
from app.repositories.orders import list_orders_for_position
from app.repositories.positions import get_posicion
from app.repositories.snapshots import list_snapshots_for_position
from app.repositories.trade_results import (
    get_trade_result_by_position,
    insertar_correccion,
    list_closed_positions_without_result,
    save_trade_result,
)

logger = logging.getLogger(__name__)

_COMPONENT = "results.trade_result_service"
_CAMPOS = tuple(f.name for f in dataclasses.fields(TradeResult))


class EstadoGeneracion(str, Enum):
    CREADO = "CREADO"
    YA_EXISTIA = "YA_EXISTIA"
    NO_CERRADA = "NO_CERRADA"


@dataclass(frozen=True)
class ResultadoGeneracion:
    position_id: uuid.UUID
    estado: EstadoGeneracion
    trade_result_id: uuid.UUID | None = None


class EstadoCorreccion(str, Enum):
    CORREGIDO = "CORREGIDO"
    """Se registró una versión nueva (el recálculo difería de la vigente)."""
    SIN_CAMBIOS = "SIN_CAMBIOS"
    """El recálculo coincide con la versión vigente: no se registra nada."""
    SIN_RESULTADO = "SIN_RESULTADO"
    """La posición no tiene un resultado que corregir."""


@dataclass(frozen=True)
class ResultadoCorreccion:
    position_id: uuid.UUID
    estado: EstadoCorreccion
    version_anterior_id: uuid.UUID | None = None
    version_nueva_id: uuid.UUID | None = None
    campos_cambiados: tuple[str, ...] = ()


def to_domain(row: TradeResultModel) -> TradeResult:
    """Fila de `trade_results` -> `TradeResult` del dominio (para las métricas)."""
    return TradeResult(**{nombre: getattr(row, nombre) for nombre in _CAMPOS})


def _recalcular_resultado(
    session: Session,
    posicion,
    *,
    initial_capital: Decimal,
    exit_prices: Mapping[str, Decimal] | None,
) -> TradeResult:
    """Reúne los hechos de la posición cerrada y llama a la función pura
    `calculate_trade_result`. Lo usan tanto la primera generación como la
    corrección auditable: una corrección SIEMPRE recalcula aquí, nunca fija
    números a mano (P1 y §37.8)."""
    position_id = posicion.id
    ejecutadas = [o for o in list_orders_for_position(session, position_id) if o.estado == EstadoOrden.FILLED]
    entradas = [o for o in ejecutadas if o.intencion == IntencionOrden.ENTRY]
    if len(entradas) != 1:
        raise RuntimeError(f"La posición {position_id} debe tener exactamente una entrada ejecutada ({len(entradas)})")
    salidas = [o for o in ejecutadas if o.side == LadoOrden.SELL]

    def _orden(o) -> OrdenDeTrade:
        return OrdenDeTrade(o.id, o.intencion, o.filled_at, o.quantity, o.execution_price, o.fee)

    entrada = _orden(entradas[0])
    propuesta = get_propuesta(session, posicion.proposal_id)
    ultima = get_ultima_observacion_hasta(session, entrada.filled_at)

    benchmark = None
    if exit_prices is not None and posicion.reference_prices_at_entry:
        benchmark = calculate_trade_benchmark(
            parse_prices(posicion.reference_prices_at_entry), exit_prices, universe=ALLOWED_SYMBOLS
        )

    return calculate_trade_result(
        entrada,
        [_orden(o) for o in salidas],
        [PuntoDeTrayectoria(s.snapshot_time, s.market_price) for s in list_snapshots_for_position(session, position_id)],
        ContextoDeEntrada(
            position_id=position_id,
            symbol=posicion.symbol,
            capital_at_entry=ultima.equity_usd if ultima is not None else initial_capital,
            initial_stop_price=posicion.initial_stop_price,
            initial_target_price=posicion.initial_target_price,
            proposal_id=posicion.proposal_id,
            analysis_cycle_id=propuesta.ciclo_id if propuesta is not None else None,
            initial_opportunity_score=propuesta.puntaje_ponderado if propuesta is not None else None,
        ),
        btc_return_pct=None if benchmark is None else benchmark.btc_return_pct,
        market_return_pct=None if benchmark is None else benchmark.market_return_pct,
    )


def generate_trade_result(
    session: Session,
    position_id: uuid.UUID,
    *,
    initial_capital: Decimal,
    exit_prices: Mapping[str, Decimal] | None = None,
) -> ResultadoGeneracion:
    """Calcula y guarda el resultado de una posición cerrada (ver el docstring
    del módulo). `exit_prices` son los precios de referencia observados al
    cerrar; sin ellos, los benchmarks quedan `NULL`."""
    posicion = get_posicion(session, position_id)
    if posicion is None:
        raise ValueError(f"No existe la posición {position_id}")
    if posicion.status != EstadoPosicion.CLOSED:
        return ResultadoGeneracion(position_id, EstadoGeneracion.NO_CERRADA)

    existente = get_trade_result_by_position(session, position_id)
    if existente is not None:
        return ResultadoGeneracion(position_id, EstadoGeneracion.YA_EXISTIA, existente.id)

    resultado = _recalcular_resultado(session, posicion, initial_capital=initial_capital, exit_prices=exit_prices)
    trade_result_id, creado = save_trade_result(session, {n: getattr(resultado, n) for n in _CAMPOS})
    logger.info(
        "trade_result %s",
        "creado" if creado else "ya existía",
        extra={"component": _COMPONENT, "position_id": str(position_id), "symbol": posicion.symbol},
    )
    return ResultadoGeneracion(
        position_id, EstadoGeneracion.CREADO if creado else EstadoGeneracion.YA_EXISTIA, trade_result_id
    )


def corregir_trade_result(
    session: Session,
    position_id: uuid.UUID,
    *,
    motivo: str,
    initial_capital: Decimal,
    autor: str | None = None,
    exit_prices: Mapping[str, Decimal] | None = None,
    dry_run: bool = False,
) -> ResultadoCorreccion:
    """Corrección auditable (§15.3): recalcula el resultado de una posición con
    los datos actuales y, SI difiere de la versión vigente, registra una versión
    nueva que la reemplaza sin tocarla. Exige un motivo. Nunca fija números a
    mano (§37.8): el resultado nuevo sale del mismo `calculate_trade_result`.

    `dry_run=True` calcula qué cambiaría pero NO escribe nada (para previsualizar
    antes de confirmar): devuelve `CORREGIDO` con `campos_cambiados` y
    `version_nueva_id=None`.

    Si el recálculo coincide con lo vigente, no registra nada (`SIN_CAMBIOS`):
    corregir no debe crear versiones espurias. Si la posición no tiene resultado
    aún, `SIN_RESULTADO` (primero se genera con `generate_trade_result`)."""
    if not motivo or not motivo.strip():
        raise ValueError("una corrección exige un motivo no vacío")

    # Primero el resultado: sin resultado (o sin posición) no hay nada que corregir.
    vigente = get_trade_result_by_position(session, position_id)  # solo_vigente por defecto
    posicion = get_posicion(session, position_id)
    if vigente is None or posicion is None:
        return ResultadoCorreccion(position_id, EstadoCorreccion.SIN_RESULTADO)

    recalculado = _recalcular_resultado(session, posicion, initial_capital=initial_capital, exit_prices=exit_prices)
    actual = to_domain(vigente)
    # El benchmark son precios OBSERVADOS al cerrar (no recalculables después). Sin
    # `exit_prices` nuevos se conserva el de la versión vigente: una corrección de
    # cálculo no debe borrarlo. Para corregir el benchmark hay que pasar exit_prices.
    if exit_prices is None:
        recalculado = dataclasses.replace(
            recalculado,
            btc_return_during_trade_pct=actual.btc_return_during_trade_pct,
            market_return_during_trade_pct=actual.market_return_during_trade_pct,
        )
    cambiados = tuple(n for n in _CAMPOS if getattr(recalculado, n) != getattr(actual, n))
    if not cambiados:
        return ResultadoCorreccion(position_id, EstadoCorreccion.SIN_CAMBIOS, version_anterior_id=vigente.id)

    if dry_run:
        return ResultadoCorreccion(
            position_id, EstadoCorreccion.CORREGIDO, version_anterior_id=vigente.id, campos_cambiados=cambiados
        )

    nueva_id, _creada = insertar_correccion(
        session,
        supersedes_id=vigente.id,
        valores={n: getattr(recalculado, n) for n in _CAMPOS},
        motivo=motivo,
        autor=autor,
    )
    logger.info(
        "trade_result corregido: %d campo(s) cambiaron",
        len(cambiados),
        extra={
            "component": _COMPONENT,
            "position_id": str(position_id),
            "version_anterior": str(vigente.id),
            "version_nueva": str(nueva_id),
        },
    )
    return ResultadoCorreccion(
        position_id,
        EstadoCorreccion.CORREGIDO,
        version_anterior_id=vigente.id,
        version_nueva_id=nueva_id,
        campos_cambiados=cambiados,
    )


def ensure_trade_results(session: Session, *, initial_capital: Decimal) -> tuple[int, int]:
    """Barrido de reparación: genera el resultado de toda posición cerrada que
    no lo tenga (p. ej. porque el cálculo falló justo al cerrar). Cada una va
    en su propio savepoint: una que falle no impide las demás. Devuelve
    `(creados, con_error)`. Sin precios de salida (benchmarks `NULL`)."""
    creados = errores = 0
    for posicion in list_closed_positions_without_result(session):
        try:
            with session.begin_nested():
                r = generate_trade_result(session, posicion.id, initial_capital=initial_capital)
            if r.estado == EstadoGeneracion.CREADO:
                creados += 1
        except Exception:  # noqa: BLE001 - se registra y el barrido sigue
            errores += 1
            logger.error(
                "no se pudo generar el trade_result",
                exc_info=True,
                extra={"component": _COMPONENT, "position_id": str(posicion.id)},
            )
    return creados, errores
