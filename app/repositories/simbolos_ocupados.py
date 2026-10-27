"""Símbolos "ocupados": con una posición abierta o una entrada pendiente de aprobación.

Solo lee. Es la MISMA definición que usa `app.execution.order_manager.build_cupos`
para `SituacionDeCupos.simbolos_abiertos` (la validación de riesgo rechaza con
`SIMBOLO_YA_ABIERTO` cualquier entrada sobre estos símbolos); un test de integración
comprueba que las dos coinciden. El pre-filtro la usa para no gastar IA en símbolos en
los que el sistema no puede entrar (decisión del 2026-10-26).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.enums import EstadoOrden, IntencionOrden
from app.repositories.orders import list_orders
from app.repositories.positions import list_open_posiciones


def simbolos_ocupados(session: Session) -> frozenset[str]:
    """Símbolos con posición `OPEN` (de CUALQUIER serie: estrategia u observación) o con
    una orden de entrada `PENDING_APPROVAL`."""
    abiertas = list_open_posiciones(session)
    pendientes = list_orders(session, estados=[EstadoOrden.PENDING_APPROVAL], intencion=IntencionOrden.ENTRY)
    return frozenset([p.symbol for p in abiertas] + [o.symbol for o in pendientes])
