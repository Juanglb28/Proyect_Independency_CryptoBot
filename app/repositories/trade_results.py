"""Persistencia de `trade_results` (Paso 9; sección 8.4 del plan).

Esta capa SOLO persiste (sección 15.2): el resultado ya viene calculado por
`app/domain/rules/results.py`. Garantías de acceso a datos:

- `save_trade_result` es idempotente por `position_id` (sección 15.7: "una
  posición cerrada produce un único `trade_result` definitivo"): reintentar
  devuelve el resultado que ya existía y NO lo modifica.
- Solo-agregar: no hay funciones de actualización ni borrado, y un trigger de
  la base las rechazaría de todos modos.

La transacción la maneja quien llama.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from app.db.models.positions import Posicion
from app.db.models.trade_results import TradeResultModel
from app.domain.enums import EstadoPosicion, SerieOperacion
from app.repositories.series import filtro_serie

_COLUMNAS = frozenset(c.name for c in TradeResultModel.__table__.columns) - {"id", "created_at"}
# Columnas del versionado: las llena la corrección, no un guardado normal.
_COLUMNAS_CORRECCION = frozenset({"supersedes_id", "correction_reason", "corrected_by"})
_COLUMNAS_RESULTADO = _COLUMNAS - _COLUMNAS_CORRECCION


def _es_vigente(modelo=TradeResultModel):
    """Predicado SQL: la fila que NINGUNA otra corrige (la punta de la cadena)."""
    hija = aliased(TradeResultModel)
    return ~select(hija.id).where(hija.supersedes_id == modelo.id).exists()


def save_trade_result(session: Session, valores: Mapping[str, Any]) -> tuple[uuid.UUID, bool]:
    """Inserta el resultado ORIGINAL de una posición cerrada. Devuelve
    `(id, creado)`; `creado=False` si esa posición ya tenía original (reintento).
    Para corregir un resultado ya existente, usar `insertar_correccion`."""
    desconocidas = set(valores) - _COLUMNAS_RESULTADO
    if desconocidas:
        raise ValueError(f"columnas desconocidas para trade_results: {sorted(desconocidas)}")
    stmt = (
        pg_insert(TradeResultModel)
        .values(id=uuid.uuid4(), **valores)
        # Conflicto contra el índice parcial del ORIGINAL (un original por posición).
        .on_conflict_do_nothing(index_elements=["position_id"], index_where=text("supersedes_id IS NULL"))
        .returning(TradeResultModel.id)
    )
    nuevo = session.execute(stmt).scalar_one_or_none()
    if nuevo is not None:
        return nuevo, True
    existente = get_trade_result_by_position(session, valores["position_id"], solo_vigente=False, solo_original=True)
    if existente is None:  # pragma: no cover - solo si la fila se borró entre ambas sentencias
        raise RuntimeError("trade_result en conflicto pero no encontrado")
    return existente.id, False


def insertar_correccion(
    session: Session,
    *,
    supersedes_id: uuid.UUID,
    valores: Mapping[str, Any],
    motivo: str,
    autor: str | None = None,
) -> tuple[uuid.UUID, bool]:
    """Inserta una CORRECCIÓN: una fila nueva que reemplaza a `supersedes_id`
    sin tocarla (§15.3). `valores` son los campos del resultado recalculado.
    Idempotente por `supersedes_id` (un reintento no crea dos correcciones de la
    misma versión): devuelve `(id, creada)`."""
    if not motivo or not motivo.strip():
        raise ValueError("una corrección exige un motivo no vacío")
    desconocidas = set(valores) - _COLUMNAS_RESULTADO
    if desconocidas:
        raise ValueError(f"columnas desconocidas para trade_results: {sorted(desconocidas)}")
    stmt = (
        pg_insert(TradeResultModel)
        .values(
            id=uuid.uuid4(),
            supersedes_id=supersedes_id,
            correction_reason=motivo.strip(),
            corrected_by=autor,
            **valores,
        )
        .on_conflict_do_nothing(index_elements=["supersedes_id"], index_where=text("supersedes_id IS NOT NULL"))
        .returning(TradeResultModel.id)
    )
    nuevo = session.execute(stmt).scalar_one_or_none()
    if nuevo is not None:
        return nuevo, True
    existente = session.execute(
        select(TradeResultModel.id).where(TradeResultModel.supersedes_id == supersedes_id)
    ).scalar_one_or_none()
    if existente is None:  # pragma: no cover
        raise RuntimeError("corrección en conflicto pero no encontrada")
    return existente, False


def get_trade_result_by_position(
    session: Session,
    position_id: uuid.UUID,
    *,
    solo_vigente: bool = True,
    solo_original: bool = False,
) -> TradeResultModel | None:
    """Resultado de una posición. Por defecto la versión VIGENTE (la punta de la
    cadena de correcciones). `solo_original=True` devuelve el original (para la
    idempotencia del guardado)."""
    stmt = select(TradeResultModel).where(TradeResultModel.position_id == position_id)
    if solo_original:
        stmt = stmt.where(TradeResultModel.supersedes_id.is_(None))
    elif solo_vigente:
        stmt = stmt.where(_es_vigente())
    return session.execute(stmt).scalar_one_or_none()


def list_versions_for_position(session: Session, position_id: uuid.UUID) -> list[TradeResultModel]:
    """Todas las versiones de una posición, del original a la más reciente
    (para la auditoría). El orden es por antigüedad de creación."""
    return list(
        session.execute(
            select(TradeResultModel)
            .where(TradeResultModel.position_id == position_id)
            .order_by(TradeResultModel.created_at, TradeResultModel.id)
        ).scalars()
    )


def list_trade_results(
    session: Session,
    *,
    exit_desde: datetime | None = None,
    exit_hasta: datetime | None = None,
    symbol: str | None = None,
    solo_vigentes: bool = True,
    serie: SerieOperacion | None = None,
) -> list[TradeResultModel]:
    """Resultados cuya SALIDA cae en `[exit_desde, exit_hasta]` (ambos
    inclusivos si se indican), en orden cronológico de salida. Por defecto solo
    las versiones VIGENTES (las métricas nunca cuentan versiones corregidas ni
    las originales ya superadas); `solo_vigentes=False` trae todas (auditoría).
    `serie` limita a la estrategia o a la observación (decisión del
    2026-10-12); sin ella devuelve las dos."""
    stmt = select(TradeResultModel).order_by(TradeResultModel.exit_time, TradeResultModel.id)
    if solo_vigentes:
        stmt = stmt.where(_es_vigente())
    if exit_desde is not None:
        stmt = stmt.where(TradeResultModel.exit_time >= exit_desde)
    if exit_hasta is not None:
        stmt = stmt.where(TradeResultModel.exit_time <= exit_hasta)
    if symbol is not None:
        stmt = stmt.where(TradeResultModel.symbol == symbol)
    if serie is not None:
        stmt = stmt.where(filtro_serie(TradeResultModel.proposal_id, serie))
    return list(session.execute(stmt).scalars())


def list_closed_positions_without_result(session: Session) -> list[Posicion]:
    """Posiciones `CLOSED` que todavía no tienen `trade_result` (por si el
    cálculo falló justo al cerrar), de la más antigua a la más reciente."""
    return list(
        session.execute(
            select(Posicion)
            .outerjoin(TradeResultModel, TradeResultModel.position_id == Posicion.id)
            .where(Posicion.status == EstadoPosicion.CLOSED, TradeResultModel.id.is_(None))
            .order_by(Posicion.closed_at, Posicion.id)
        ).scalars()
    )
