"""Agente 2 — Analista cuantitativo (sección 7.1/7.2 del plan).

Interpreta indicadores YA calculados (Paso 4) de un símbolo. NO calcula
nada y no tiene acceso a ninguna herramienta de ejecución.

`datos` (el bloque DATOS del prompt) lo arma quien llama a esta función —
"La IA recibe únicamente datos preparados" (sección 15.10 del plan, Paso
6). Armar ese bloque a partir de las tablas reales `velas`/`indicadores`
para un símbolo concreto es responsabilidad de la orquestación (Paso 7,
`app/orchestration/candidate_pipeline.py`, todavía no implementado) — este
módulo solo sabe qué prompt, qué modelo y qué schema usar.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.ai.agents._runner import AgentRunResult, run_json_agent
from app.ai.models import MODEL_ANALISTA
from app.ai.prompts.quantitative import PROMPT_VERSION, SYSTEM_PROMPT
from app.ai.schemas import AnalistaCuantitativoOutput
from app.config import Settings

AGENTE = "analista_cuantitativo"

MAX_TOKENS_ANALISTA_CUANTITATIVO = 8_000
"""Preventivo, no un bug confirmado todavía (a diferencia de
`events.MAX_TOKENS_ANALISTA_EVENTOS`, que sí se cortó en producción el
2026-10-02): `observaciones` tiene la misma forma estructural de riesgo —
una lista que crece con la cantidad de factores/temporalidades que el
agente decida comentar, con texto libre por ítem. Subir el tope ahora, en
vez de esperar a que falle en una corrida real, no cuesta nada (un TOPE no
es un gasto garantizado — ver `consolidator.MAX_TOKENS_CONSOLIDADOR`)."""


def run_analista_cuantitativo(
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
        schema=AnalistaCuantitativoOutput,
        ciclo_id=ciclo_id,
        max_tokens=MAX_TOKENS_ANALISTA_CUANTITATIVO,
    )
