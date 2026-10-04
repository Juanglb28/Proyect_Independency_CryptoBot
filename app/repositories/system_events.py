"""Persistencia de `eventos_sistema` (Paso 8; decisión del usuario del
2026-10-04). Ver `app/db/models/system_events.py` para qué guarda y por qué.

Esta capa SOLO persiste (sección 15.2). No calcula equity ni pico, no decide
cuándo pausar (eso es de `app/domain/rules/risk.py`) ni interpreta si el
sistema está pausado (`risk.sistema_pausado` recibe el tipo del último
evento que devuelve `get_ultimo_evento_sistema`).

`append_evento_sistema` protege las transiciones válidas del estado, igual
que `resolve_pending_order` protege las suyas:

- `PAUSA_DRAWDOWN` solo se agrega si no hay eventos o el último es
  `REANUDACION`;
- `REANUDACION` solo se agrega si el último es `PAUSA_DRAWDOWN`.

Cualquier otra combinación NO escribe nada y devuelve `False`: así un
reintento tras una caída (o un monitor que detecta el mismo drawdown dos
veces) no duplica pausas ni reanuda un sistema que no estaba pausado. Para
que dos escrituras concurrentes no se crucen entre la lectura del último
evento y el insert, se toma un candado de asesor de Postgres a nivel de
transacción (se libera solo al terminar la transacción).

La transacción la maneja quien llama.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models.system_events import EventoSistema
from app.domain.enums import TipoEventoSistema

# Clave arbitraria pero fija del candado de asesor que serializa las
# escrituras en `eventos_sistema`. No colisiona con nada: ningún otro código
# del proyecto usa candados de asesor.
_LOCK_KEY_EVENTOS_SISTEMA = 8_200_410_001

_TRANSICIONES_VALIDAS: dict[TipoEventoSistema, frozenset[TipoEventoSistema | None]] = {
    TipoEventoSistema.PAUSA_DRAWDOWN: frozenset({None, TipoEventoSistema.REANUDACION}),
    TipoEventoSistema.REANUDACION: frozenset({TipoEventoSistema.PAUSA_DRAWDOWN}),
}


@dataclass(frozen=True)
class EventoSistemaRow:
    """Un evento listo para persistir. Las restricciones de la tabla exigen
    que una `REANUDACION` traiga `previous_peak_equity_usd` y que su
    `peak_equity_usd` sea igual a `equity_usd`."""

    event_type: TipoEventoSistema
    occurred_at: datetime
    equity_usd: Decimal
    peak_equity_usd: Decimal
    previous_peak_equity_usd: Decimal | None = None
    detail: str | None = None


def get_ultimo_evento_sistema(session: Session) -> EventoSistema | None:
    """El evento más reciente (por `id`, no por reloj) o `None` si nunca hubo
    ninguno."""
    return session.execute(
        select(EventoSistema).order_by(EventoSistema.id.desc()).limit(1)
    ).scalar_one_or_none()


def append_evento_sistema(session: Session, row: EventoSistemaRow) -> bool:
    """Agrega el evento si la transición es válida (ver el docstring del
    módulo). Devuelve `True` si lo escribió y `False` si no hizo nada."""
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY_EVENTOS_SISTEMA})

    ultimo = get_ultimo_evento_sistema(session)
    tipo_ultimo = ultimo.event_type if ultimo is not None else None
    if tipo_ultimo not in _TRANSICIONES_VALIDAS[row.event_type]:
        return False

    session.add(
        EventoSistema(
            event_type=row.event_type,
            occurred_at=row.occurred_at,
            equity_usd=row.equity_usd,
            peak_equity_usd=row.peak_equity_usd,
            previous_peak_equity_usd=row.previous_peak_equity_usd,
            detail=row.detail,
        )
    )
    session.flush()
    return True


def list_eventos_sistema(session: Session) -> list[EventoSistema]:
    """Todos los eventos, del más antiguo al más reciente."""
    return list(session.execute(select(EventoSistema).order_by(EventoSistema.id)).scalars())
