"""Agente 4 — Auditor de datos (sección 7.1/7.2 del plan).

Evalúa completitud/antigüedad/huecos/anomalías de los datos que van a
sustentar una decisión — no analiza mercados, no calcula nada. Es el más
barato de los cinco (Haiku), coherente con ser una verificación mecánica.

`datos` lo arma quien llama — ver el docstring de `quantitative.py`.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.ai.agents._runner import AgentRunResult, run_json_agent
from app.ai.models import MODEL_AUDITOR
from app.ai.prompts.auditor import PROMPT_VERSION, SYSTEM_PROMPT
from app.ai.schemas import AuditorOutput
from app.config import Settings

AGENTE = "auditor_datos"


def run_auditor_datos(
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
        modelo=MODEL_AUDITOR,
        prompt_version=PROMPT_VERSION,
        system_prompt=SYSTEM_PROMPT,
        datos=datos,
        schema=AuditorOutput,
        ciclo_id=ciclo_id,
    )
