"""Filtro por SERIE de operación (estrategia / observación), decisión del
usuario del 2026-10-12.

La serie no es una columna: se deriva del `tier_entrada` de la propuesta de la
que sale la operación (`OBSERVACION` -> observación; cualquier otro valor o NULL
-> estrategia). Una sola fuente de verdad, así no hay dos campos que puedan
contradecirse. Solo persistencia: aquí no hay ninguna regla de negocio.
"""

from __future__ import annotations

from sqlalchemy import exists
from sqlalchemy.sql.elements import ColumnElement

from app.db.models.analysis import Propuesta
from app.domain.enums import SerieOperacion, TierEntrada


def filtro_serie(columna_proposal_id, serie: SerieOperacion) -> ColumnElement[bool]:
    """Condición SQL: la propuesta apuntada por `columna_proposal_id` es de `serie`.

    Una operación sin propuesta (`proposal_id` NULL, p. ej. un `trade_result`
    anterior a esa columna) cuenta como ESTRATEGIA: nunca es una observación,
    porque las observaciones siempre nacen de su propia propuesta.
    """
    es_observacion = exists().where(
        Propuesta.id == columna_proposal_id, Propuesta.tier_entrada == TierEntrada.OBSERVACION
    )
    return es_observacion if serie == SerieOperacion.OBSERVACION else ~es_observacion
