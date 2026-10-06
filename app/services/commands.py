"""Acciones manuales de la interfaz (Paso 10c): aprobar o rechazar una entrada
pendiente y reanudar el sistema tras una pausa por drawdown.

Es la ÚNICA parte de `app/services` que escribe, y solo llama a las funciones
deterministas que ya usan los comandos de terminal (`scripts.entries` y
`scripts.system_control`): no hay reglas nuevas aquí. Las lecturas de la
interfaz siguen en `app.services.views` (transacción READ ONLY); ningún módulo
de lectura importa este.

Cada acción es una sola transacción: si algo falla, no queda nada a medias, y
la interfaz recibe un mensaje claro (nunca una traza). Aprobar abre la
posición paper al precio de Binance de ESTE momento (decisión del Paso 8), con
el ticker público: no usa credenciales. Ni aprobar ni reanudar pueden saltarse
la pausa o el archivo PARAR: esas comprobaciones viven en `app.execution`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.session import session_scope
from app.execution.order_manager import ResultadoAprobacion, approve_pending_entry, reject_pending_entry
from app.execution.price_source import BinanceTickerPriceSource, PriceSource
from app.execution.system_control import ResultadoReanudacion, resume_system
from app.services._escritura import ResultadoAccion, escritura_factory
from app.services.session import settings

logger = logging.getLogger(__name__)
_COMPONENT = "services.commands"


_MENSAJES_APROBACION = {
    ResultadoAprobacion.APROBADA: "Entrada aprobada y ejecutada en paper.",
    ResultadoAprobacion.EXPIRADA: "La entrada había vencido: quedó expirada y NO se ejecutó.",
    ResultadoAprobacion.NO_PENDIENTE: "La orden ya no estaba pendiente; no se hizo nada.",
    ResultadoAprobacion.BLOQUEADA_POR_PAUSA: (
        "El sistema está pausado por drawdown: no se abren entradas nuevas. "
        "La orden sigue pendiente (puedes rechazarla)."
    ),
    ResultadoAprobacion.BLOQUEADA_POR_PARADA: (
        "Existe el archivo PARAR: no se abren entradas nuevas. La orden sigue pendiente (puedes rechazarla)."
    ),
    ResultadoAprobacion.SIN_PRECIO: "No se pudo obtener un precio válido de Binance; la orden sigue pendiente.",
}

_ERROR_GENERICO = "No se pudo completar la acción y no se cambió nada. Revisa los registros e inténtalo de nuevo."


@contextmanager
def _abrir_sesion() -> Iterator[Session]:
    """Sesión de ESCRITURA (commit al salir bien, rollback si falla)."""
    with session_scope(escritura_factory()) as session:
        yield session


def _fuente_de_precios() -> PriceSource:
    return BinanceTickerPriceSource()


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _usd(valor: Decimal) -> str:
    return format(valor.quantize(Decimal("0.01")), "f")


def aprobar_entrada(orden_id: uuid.UUID) -> ResultadoAccion:
    """Aprueba una entrada pendiente (mismas reglas que `scripts.entries approve`)."""
    try:
        with _abrir_sesion() as session:
            resultado = approve_pending_entry(
                session, orden_id, now=_ahora(), price_source=_fuente_de_precios()
            )
    except Exception:  # noqa: BLE001 — la interfaz recibe un mensaje, el detalle va al log
        logger.exception("accion de interfaz fallida", extra={"component": _COMPONENT, "accion": "aprobar"})
        return ResultadoAccion(False, "ERROR", _ERROR_GENERICO)
    logger.info(
        "accion de interfaz", extra={"component": _COMPONENT, "accion": "aprobar", "orden_id": str(orden_id),
                                      "resultado": resultado.value}
    )
    return ResultadoAccion(resultado == ResultadoAprobacion.APROBADA, resultado.value, _MENSAJES_APROBACION[resultado])


def rechazar_entrada(orden_id: uuid.UUID, motivo: str | None = None) -> ResultadoAccion:
    """Rechaza una entrada pendiente y libera lo que reservaba (permitido aun en pausa)."""
    detalle = (motivo or "").strip() or None
    try:
        with _abrir_sesion() as session:
            rechazada = reject_pending_entry(session, orden_id, now=_ahora(), detail=detalle)
    except Exception:  # noqa: BLE001
        logger.exception("accion de interfaz fallida", extra={"component": _COMPONENT, "accion": "rechazar"})
        return ResultadoAccion(False, "ERROR", _ERROR_GENERICO)
    logger.info(
        "accion de interfaz", extra={"component": _COMPONENT, "accion": "rechazar", "orden_id": str(orden_id),
                                      "resultado": "RECHAZADA" if rechazada else "NO_PENDIENTE"}
    )
    if rechazada:
        return ResultadoAccion(True, "RECHAZADA", "Entrada rechazada. Sus cupos quedaron libres.")
    return ResultadoAccion(False, "NO_PENDIENTE", "La orden ya no estaba pendiente; no se hizo nada.")


def reanudar_sistema() -> ResultadoAccion:
    """Reanuda tras una pausa por drawdown (mismas reglas que `scripts.system_control resume`):
    el pico de equity se reinicia al equity de este momento."""
    try:
        with _abrir_sesion() as session:
            resultado, equity = resume_system(
                session, now=_ahora(), price_source=_fuente_de_precios(),
                initial_capital=settings().paper_capital_usd,
            )
    except Exception:  # noqa: BLE001
        logger.exception("accion de interfaz fallida", extra={"component": _COMPONENT, "accion": "reanudar"})
        return ResultadoAccion(False, "ERROR", _ERROR_GENERICO)
    logger.warning(
        "accion de interfaz", extra={"component": _COMPONENT, "accion": "reanudar", "resultado": resultado.value}
    )
    if resultado == ResultadoReanudacion.REANUDADO:
        return ResultadoAccion(
            True, resultado.value,
            f"Sistema reanudado. El pico de equity se reinició a {_usd(equity)} USD (el anterior quedó registrado).",
        )
    if resultado == ResultadoReanudacion.NO_PAUSADO:
        return ResultadoAccion(False, resultado.value, "El sistema no estaba pausado por drawdown; no se hizo nada.")
    return ResultadoAccion(
        False, resultado.value,
        "No se pudo obtener el precio de una posición abierta; no se reanudó (no se inventa un precio). Inténtalo de nuevo.",
    )
