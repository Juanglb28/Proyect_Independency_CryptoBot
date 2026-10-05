"""Persistencia de `equity_observaciones` (Paso 8; decisión del usuario del
2026-10-05). Ver `app/db/models/equity.py` para qué guarda y por qué.

Esta capa SOLO persiste (sección 15.2). No calcula equity ni pico y no decide
cuándo pausar (eso es de `app/domain/rules/risk.py`): guarda y devuelve lo
que el servicio le entrega.

El pico vigente es el de la ÚLTIMA fila (por `id`). Para que el monitor y una
reanudación manual no se crucen entre "leer el pico" y "escribir la
observación", `acquire_equity_lock` toma un candado de asesor de Postgres a
nivel de transacción (se libera solo al terminar la transacción y es
reentrante dentro de la misma sesión). El monitor lo toma al empezar el
chequeo; `append_observacion` lo vuelve a tomar por si lo llaman solos.

La transacción la maneja quien llama.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models.equity import EquityObservacion
from app.domain.enums import OrigenObservacionEquity

# Clave arbitraria pero fija, distinta de la de `eventos_sistema`.
_LOCK_KEY_EQUITY = 8_200_410_002


@dataclass(frozen=True)
class EquityObservacionRow:
    """Una observación lista para persistir. La base exige
    `peak_equity_usd >= equity_usd` y, si el origen es `REANUDACION`, que el
    pico sea igual al equity."""

    observed_at: datetime
    origen: OrigenObservacionEquity
    equity_usd: Decimal
    peak_equity_usd: Decimal
    open_positions: int


def acquire_equity_lock(session: Session) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY_EQUITY})


def get_ultima_observacion(session: Session) -> EquityObservacion | None:
    """La observación más reciente (por `id`, no por reloj) o `None` si nunca
    hubo ninguna."""
    return session.execute(
        select(EquityObservacion).order_by(EquityObservacion.id.desc()).limit(1)
    ).scalar_one_or_none()


def append_observacion(session: Session, row: EquityObservacionRow) -> int:
    """Agrega la observación y devuelve su `id`."""
    acquire_equity_lock(session)
    observacion = EquityObservacion(
        observed_at=row.observed_at,
        origen=row.origen,
        equity_usd=row.equity_usd,
        peak_equity_usd=row.peak_equity_usd,
        open_positions=row.open_positions,
    )
    session.add(observacion)
    session.flush()
    return observacion.id


def list_observaciones(session: Session) -> list[EquityObservacion]:
    """Todas las observaciones, de la más antigua a la más reciente."""
    return list(session.execute(select(EquityObservacion).order_by(EquityObservacion.id)).scalars())


def list_observaciones_between(session: Session, *, desde: datetime, hasta: datetime) -> list[EquityObservacion]:
    """Observaciones con `observed_at` en `[desde, hasta]` (inclusivos), de la
    más antigua a la más reciente (empates por `id`)."""
    return list(
        session.execute(
            select(EquityObservacion)
            .where(EquityObservacion.observed_at >= desde, EquityObservacion.observed_at <= hasta)
            .order_by(EquityObservacion.observed_at, EquityObservacion.id)
        ).scalars()
    )


def get_ultima_observacion_hasta(session: Session, hasta: datetime) -> EquityObservacion | None:
    """La última observación con `observed_at <= hasta`, o `None`."""
    return session.execute(
        select(EquityObservacion)
        .where(EquityObservacion.observed_at <= hasta)
        .order_by(EquityObservacion.observed_at.desc(), EquityObservacion.id.desc())
        .limit(1)
    ).scalar_one_or_none()
