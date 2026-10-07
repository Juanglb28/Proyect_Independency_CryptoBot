"""Envío de los avisos guardados en `alertas` (Paso 11a).

Se llama DESPUÉS de confirmar la transacción del chequeo, nunca dentro de ella
ni en el camino de una orden (P2). Cada aviso va en SU PROPIA transacción:
se reserva la fila (`FOR UPDATE SKIP LOCKED`), se envía y se anota el recibo.

Garantía: al menos una vez. Si el proceso muere justo entre el envío y la
anotación del recibo, ese aviso puede enviarse otra vez al reiniciar; no se
pierde ninguno. Un fallo de envío no se propaga: se anota (intento y motivo) y
se reintenta en la siguiente llamada, hasta `MAX_INTENTOS_ENTREGA`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.notifications.telegram import Sender, TelegramError
from app.repositories.alertas import (
    claim_siguiente_pendiente,
    record_entrega_exitosa,
    record_entrega_fallida,
)

logger = logging.getLogger(__name__)

_COMPONENT = "notifications.dispatcher"

# Valores operativos (no financieros): cuántas veces se reintenta un envío
# fallido y cuántos avisos se envían por llamada. Un aviso que agota los
# intentos se queda sin entregar y visible en la tabla (último error).
MAX_INTENTOS_ENTREGA = 5
LOTE = 20
_LARGO_MAX_ERROR = 500


@dataclass(frozen=True)
class ResultadoDespacho:
    enviadas: int = 0
    fallidas: int = 0


def dispatch_pending_alerts(
    session_factory: sessionmaker[Session],
    sender: Sender | None,
    *,
    now: datetime,
    max_intentos: int = MAX_INTENTOS_ENTREGA,
    lote: int = LOTE,
) -> ResultadoDespacho:
    """Envía los avisos pendientes. Sin `sender` (Telegram sin configurar) no
    hace nada: los avisos esperan en la tabla."""
    if sender is None:
        return ResultadoDespacho()

    enviadas = fallidas = 0
    ultimo_id = 0
    for _ in range(lote):
        with session_scope(session_factory) as session:
            alerta = claim_siguiente_pendiente(session, max_intentos=max_intentos, id_mayor_que=ultimo_id)
            if alerta is None:
                break
            ultimo_id = alerta.id  # un aviso que falla no se reintenta dentro de esta misma llamada
            try:
                sender.send(alerta.message)
            except TelegramError as exc:
                record_entrega_fallida(session, alerta.id, error=str(exc)[:_LARGO_MAX_ERROR])
                fallidas += 1
                logger.warning(
                    "no se pudo enviar un aviso",
                    extra={"component": _COMPONENT, "alerta_id": alerta.id, "tipo": alerta.tipo.value},
                )
            else:
                record_entrega_exitosa(session, alerta.id, now=now)
                enviadas += 1
    return ResultadoDespacho(enviadas=enviadas, fallidas=fallidas)
