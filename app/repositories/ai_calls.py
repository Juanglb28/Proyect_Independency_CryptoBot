"""Persistencia de `llamadas_ia` (sección 6 del plan, Paso 6).

Esta capa SOLO persiste (sección 15.2). No calcula el costo ni valida el
JSON del agente — eso ya viene resuelto quien llama (`app/ai/agents/*.py`
usa `app/ai/models.py` para el costo y `app/ai/schemas.py` para la
validación antes de llegar aquí).

A diferencia de `save_candles`/`save_indicators`/`save_event_analysis`, no
hay aquí una clave de idempotencia natural que prevenga duplicados: cada
llamada real a la API es un evento único que ocurrió una vez, no un hecho
recalculable con la misma clave. Un reintento tras una caída a mitad de
escritura podría, en el peor caso, registrar dos veces el costo de UNA
llamada — un riesgo aceptado por ahora porque esta tabla es de
observabilidad/auditoría (cuánto costó, qué respondió), no estado
operativo crítico como `posiciones` u `ordenes` (que sí exigen
idempotencia estricta, sección 15.7 del plan). Se documenta aquí en vez de
resolverlo con una clave arbitraria que no pediría el plan.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.ai_calls import LlamadaIA


@dataclass(frozen=True)
class LlamadaIARow:
    """Una llamada a un agente de IA, lista para persistir.

    `respuesta`/`exitosa`/`error_message` (bug real 2026-10-02, ver
    `app/db/models/ai_calls.py::LlamadaIA`): una llamada EXITOSA trae
    `respuesta` con el JSON ya validado y `exitosa=True` (el default,
    compatible con todas las llamadas de antes de este campo). Una llamada
    FALLIDA (JSON inválido, truncada por `max_tokens`, o con forma
    distinta al schema) trae `respuesta=None`, `exitosa=False` y
    `error_message` con el motivo — Anthropic la cobra igual, así que debe
    quedar registrada para que `cost_usd` no la pierda."""

    agente: str
    modelo: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    respuesta: dict | None = None
    exitosa: bool = True
    error_message: str | None = None
    ciclo_id: uuid.UUID | None = None


def save_llamada_ia(session: Session, row: LlamadaIARow) -> uuid.UUID:
    """Inserta el registro de una llamada ya realizada (exitosa o fallida —
    ver `LlamadaIARow`), y devuelve su `id`."""
    llamada = LlamadaIA(
        ciclo_id=row.ciclo_id,
        agente=row.agente,
        modelo=row.modelo,
        prompt_version=row.prompt_version,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cost_usd=row.cost_usd,
        respuesta=row.respuesta,
        exitosa=row.exitosa,
        error_message=row.error_message,
    )
    session.add(llamada)
    session.flush()
    return llamada.id


def get_gasto_acumulado_desde(session: Session, desde: datetime) -> Decimal:
    """Suma `cost_usd` de TODAS las llamadas registradas en `llamadas_ia`
    con `created_at >= desde`, exitosas o no (bug real 2026-10-02: antes de
    persistir también las fallidas, esta suma las omitía por completo,
    aunque Anthropic sí las cobra) — reconstruible en cualquier momento a
    partir de los datos ya persistidos (mismo principio que
    `performance_metrics`, sección 13 del plan: un agregado derivado, nunca
    una cifra que se acumula aparte y puede desincronizarse).

    Usado para estimar cuánto queda del saldo declarado por el usuario en
    `Settings.anthropic_saldo_declarado_usd` (ver ese campo: la API de
    Anthropic no expone un endpoint de saldo real, así que esto sigue
    siendo una ESTIMACIÓN, no la cifra exacta de la consola — en
    particular, no incluye gasto de errores que ocurren ANTES de recibir
    respuesta de la API, como una caída de red, que normalmente tampoco
    cobra Anthropic)."""
    total = session.execute(
        select(func.coalesce(func.sum(LlamadaIA.cost_usd), 0)).where(LlamadaIA.created_at >= desde)
    ).scalar_one()
    return Decimal(total)
