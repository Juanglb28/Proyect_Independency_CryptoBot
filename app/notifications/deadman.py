"""Ping al dead-man's-switch externo — sin dependencias nuevas (``urllib``).

El scheduler llama ``ping_deadman`` en cada latido (cada 5 min). Es un simple
GET a una URL opaca de un servicio externo (p. ej. healthchecks.io, o cualquier
endpoint equivalente): mientras lleguen pings, el servicio sabe que el scheduler
vive; si dejan de llegar, ESE servicio avisa por su cuenta. Como quien avisa
está fuera de la máquina, cubre el caso de computador apagado o sin energía
(sección "Vigilancia del scheduler" del README).

Reglas:
- Es "best-effort": NUNCA lanza. Un fallo de red no puede tumbar el latido ni el
  scheduler (P5). Devuelve ``True`` si el ping salió bien, ``False`` si no.
- La URL puede llevar un identificador secreto: NUNCA se registra ni se incluye
  en un error. Si un ping falla solo se anota el tipo de error, no la URL.
- No envía datos del sistema: solo pega a la URL. La privacidad del ping la
  decide el usuario al elegir el servicio.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from collections.abc import Callable

logger = logging.getLogger(__name__)

_COMPONENT = "notifications.deadman"
TIMEOUT_SEGUNDOS = 10.0


def ping_deadman(
    url: str | None,
    *,
    timeout: float = TIMEOUT_SEGUNDOS,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> bool:
    """Pega a ``url`` (GET). ``None`` o vacío = no configurado, no hace nada.

    Devuelve ``True`` solo si la petición se completó sin excepción.
    """
    if not url:
        return False
    peticion = urllib.request.Request(url, method="GET")
    try:
        with opener(peticion, timeout=timeout) as respuesta:  # type: ignore[operator]
            respuesta.read()
        return True
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        # Nunca registrar la URL (puede llevar un secreto): solo el tipo de error.
        logger.warning(
            "Dead-man's-switch: el ping falló (%s)",
            type(exc).__name__,
            extra={"component": _COMPONENT},
        )
        return False
