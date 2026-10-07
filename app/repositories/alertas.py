"""Persistencia de `alertas` (Paso 11a). Ver `app/db/models/alertas.py`.

Solo persiste (sección 15.2): no decide cuándo avisar (eso es de las reglas de
escalamiento), no envía nada (eso es de `app/notifications`) y no interpreta el
estado de "Binance caído" (lo hace quien llame con `get_ultima_alerta_de_tipos`).

Idempotencia: `append_alerta` usa `INSERT ... ON CONFLICT (dedup_key) DO
NOTHING`. Un reintento, o un reinicio a mitad de un chequeo, no crea un aviso
repetido: devuelve `False` y no escribe nada.

La transacción la maneja quien llama.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models.alertas import Alerta
from app.domain.enums import TipoAlerta


@dataclass(frozen=True)
class AlertaRow:
    """Un aviso listo para persistir."""

    tipo: TipoAlerta
    subject: str
    dedup_key: str
    occurred_at: datetime
    message: str
    observed_value: Decimal | None = None
    threshold: Decimal | None = None
    position_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None


def append_alerta(session: Session, row: AlertaRow) -> bool:
    """Agrega el aviso si su `dedup_key` es nueva. `True` si lo escribió,
    `False` si ya existía (no hace nada)."""
    resultado = session.execute(
        pg_insert(Alerta)
        .values(
            tipo=row.tipo,
            subject=row.subject,
            dedup_key=row.dedup_key,
            occurred_at=row.occurred_at,
            message=row.message,
            observed_value=row.observed_value,
            threshold=row.threshold,
            position_id=row.position_id,
            order_id=row.order_id,
        )
        .on_conflict_do_nothing(index_elements=["dedup_key"])
        .returning(Alerta.id)
    ).first()
    return resultado is not None


def get_ultima_alerta_de_tipos(session: Session, tipos: list[TipoAlerta]) -> Alerta | None:
    """La alerta más reciente (por `id`, no por reloj) de alguno de `tipos`."""
    return session.execute(
        select(Alerta).where(Alerta.tipo.in_(tipos)).order_by(Alerta.id.desc()).limit(1)
    ).scalar_one_or_none()


def list_alertas(session: Session, *, limit: int = 100) -> list[Alerta]:
    """Las últimas `limit` alertas, de la más reciente a la más antigua."""
    return list(session.execute(select(Alerta).order_by(Alerta.id.desc()).limit(limit)).scalars())


def claim_siguiente_pendiente(session: Session, *, max_intentos: int, id_mayor_que: int = 0) -> Alerta | None:
    """El aviso más antiguo aún no entregado, con menos de `max_intentos`
    intentos y `id` mayor que `id_mayor_que`, reservado para esta transacción
    (`FOR UPDATE SKIP LOCKED`: dos despachadores a la vez no se pisan)."""
    return session.execute(
        select(Alerta)
        .where(Alerta.delivered_at.is_(None), Alerta.delivery_attempts < max_intentos, Alerta.id > id_mayor_que)
        .order_by(Alerta.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()


def record_entrega_exitosa(session: Session, alerta_id: int, *, now: datetime) -> None:
    session.execute(
        update(Alerta)
        .where(Alerta.id == alerta_id, Alerta.delivered_at.is_(None))
        .values(delivered_at=now, delivery_attempts=Alerta.delivery_attempts + 1, last_delivery_error=None)
    )


def record_entrega_fallida(session: Session, alerta_id: int, *, error: str) -> None:
    session.execute(
        update(Alerta)
        .where(Alerta.id == alerta_id, Alerta.delivered_at.is_(None))
        .values(delivery_attempts=Alerta.delivery_attempts + 1, last_delivery_error=error)
    )
