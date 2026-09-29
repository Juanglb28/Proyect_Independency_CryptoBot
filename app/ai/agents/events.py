"""Agente 3 — Analista de eventos (sección 7.1/7.2 del plan).

Interpreta las correlaciones de `analisis_eventos` (Paso 5) para un
símbolo. Es el único de los tres agentes de este bloque que procesa texto
potencialmente no confiable (las descripciones de eventos del catálogo) —
P4 del plan: por eso este agente tampoco recibe ninguna herramienta de
ejecución (ver `app/ai/client.py`), la separación es arquitectónica, no
solo la advertencia dentro del prompt.

`datos` lo arma quien llama — ver el docstring de `quantitative.py` sobre
por qué eso queda para el Paso 7 (orquestación), no para este módulo.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.ai.agents._runner import AgentRunResult, run_json_agent
from app.ai.models import MODEL_ANALISTA
from app.ai.prompts.events import PROMPT_VERSION, SYSTEM_PROMPT
from app.ai.schemas import AnalistaEventosOutput
from app.config import Settings

AGENTE = "analista_eventos"


def run_analista_eventos(
    session: Session,
    settings: Settings,
    datos: dict,
    *,
    ciclo_id: uuid.UUID | None = None,
) -> AgentRunResult:
    return run_json_agent(
        session,
        settings,
        agente=AGENTE,
        modelo=MODEL_ANALISTA,
        prompt_version=PROMPT_VERSION,
        system_prompt=SYSTEM_PROMPT,
        datos=datos,
        schema=AnalistaEventosOutput,
        ciclo_id=ciclo_id,
    )
