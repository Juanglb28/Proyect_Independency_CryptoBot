"""Agente 5 — Consolidador (sección 7.1/7.2 del plan).

A diferencia de los otros tres agentes de este bloque, aquí SÍ hay trabajo
después de validar el schema: el agente entrega su juicio de 4 categorías
por opción (0-100 cada una), y este módulo:

1. Verifica que cada `fuentes` referencie un evento REAL del catálogo
   (nunca un id inventado — sección P3 del plan).
2. Calcula `puntaje_base`/`puntaje_ponderado` con
   `app.domain.rules.scoring.calculate_consolidated_score` — nunca deja
   que el LLM haga esa aritmética (P1 del plan). `calidad_datos` no lo
   juzga el Consolidador: lo trae quien llama a esta función a partir del
   Auditor (típicamente `100 - AuditorOutput.penalizacion_confianza_pct`),
   en `calidad_datos_por_simbolo`.

`datos` (el bloque DATOS del prompt: las salidas ya validadas de los
Agentes 2/3/4) lo arma quien llama — igual que en `quantitative.py`/
`events.py`/`auditor.py`, eso es responsabilidad del Paso 7
(orquestación), no de este módulo.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.ai.agents._runner import AgentRunResult, AgentValidationError, run_json_agent
from app.ai.models import MODEL_CONSOLIDADOR
from app.ai.prompts.consolidator import PROMPT_VERSION, SYSTEM_PROMPT
from app.ai.schemas import ConsolidadorOutput, OpcionConsolidada
from app.config import Settings
from app.domain.rules.scoring import ScoreConsolidado, calculate_consolidated_score
from app.repositories.events import get_existing_external_ids

AGENTE = "consolidador"

MAX_TOKENS_CONSOLIDADOR = 16_000
"""Muy por encima del default de `run_json_agent` (4096) a propósito: a
diferencia de los agentes 2/3/4 (un símbolo por llamada), la salida del
Consolidador crece con la cantidad de candidatos (hasta 5, cada uno con
tesis/invalidación/escenario adverso en texto libre). `max_tokens` es un
TOPE, no un gasto garantizado — no cuesta más pedir de más si el modelo
genera menos — así que no hay motivo para dejarlo ajustado.

BUG REAL (2026-09-30): con el default de 4096, una corrida real de
`scripts/run_daily_cycle.py` con varios candidatos cortó la respuesta del
Consolidador a mitad de una cadena JSON (`stop_reason="max_tokens"`,
detectado ahora explícitamente en `app.ai.client.call_agent`), lo que
marcó el ciclo entero como fallido después de haber pagado esa llamada."""


@dataclass(frozen=True)
class OpcionConsolidadaEvaluada:
    """Una opción del Consolidador, ya con su puntaje calculado en código
    (nunca por el agente) — lo que de verdad se usaría para decidir."""

    opcion: OpcionConsolidada
    score: ScoreConsolidado


@dataclass(frozen=True)
class ConsolidadorRunResult:
    agent_result: AgentRunResult
    opciones_evaluadas: tuple[OpcionConsolidadaEvaluada, ...]

    @property
    def output(self) -> ConsolidadorOutput:
        return self.agent_result.output  # type: ignore[return-value]


def _validar_fuentes(session: Session, output: ConsolidadorOutput) -> None:
    referenciados = {fuente for opcion in output.opciones for fuente in opcion.fuentes}
    if not referenciados:
        return
    existentes = get_existing_external_ids(session, referenciados)
    inexistentes = referenciados - existentes
    if inexistentes:
        raise AgentValidationError(
            f"El Consolidador citó en 'fuentes' eventos que no existen en el catálogo: "
            f"{sorted(inexistentes)}"
        )


def run_consolidador(
    session: Session,
    settings: Settings,
    datos: dict,
    calidad_datos_por_simbolo: dict[str, Decimal],
    *,
    ciclo_id: uuid.UUID | None = None,
) -> ConsolidadorRunResult:
    """`calidad_datos_por_simbolo` debe tener una entrada (0-100) por cada
    símbolo que el agente pueda proponer en `opciones` — si falta alguno,
    lanza `KeyError` en vez de asumir un valor por defecto (sección P3del
    plan: nunca inventar un dato faltante, ni siquiera para no bloquear)."""

    def extra_validate(output: ConsolidadorOutput) -> None:
        _validar_fuentes(session, output)

    agent_result = run_json_agent(
        session,
        settings,
        agente=AGENTE,
        modelo=MODEL_CONSOLIDADOR,
        prompt_version=PROMPT_VERSION,
        system_prompt=SYSTEM_PROMPT,
        datos=datos,
        schema=ConsolidadorOutput,
        ciclo_id=ciclo_id,
        extra_validate=extra_validate,
        max_tokens=MAX_TOKENS_CONSOLIDADOR,
    )

    output: ConsolidadorOutput = agent_result.output  # type: ignore[assignment]
    opciones_evaluadas = tuple(
        OpcionConsolidadaEvaluada(
            opcion=opcion,
            score=calculate_consolidated_score(
                tendencia_momentum=Decimal(str(opcion.tendencia_momentum)),
                volumen_liquidez=Decimal(str(opcion.volumen_liquidez)),
                posicion_rangos_ath=Decimal(str(opcion.posicion_rangos_ath)),
                contexto_eventos=Decimal(str(opcion.contexto_eventos)),
                calidad_datos=Decimal(calidad_datos_por_simbolo[opcion.simbolo]),
            ),
        )
        for opcion in output.opciones
    )

    return ConsolidadorRunResult(agent_result=agent_result, opciones_evaluadas=opciones_evaluadas)
