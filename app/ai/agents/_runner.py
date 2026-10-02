"""Orquestación compartida por los agentes JSON-in/JSON-out (2, 3 y 4).

No es un agente por sí mismo (por eso el guion bajo: no aparece en la
sección 15.1 del plan, que solo lista `interviewer.py`, `quantitative.py`,
`events.py`, `auditor.py`, `consolidator.py`). Los tres agentes de este
Paso 6 comparten exactamente los mismos cinco pasos — llamar al modelo,
parsear el JSON, validarlo contra su schema, calcular el costo y guardar el
registro en `llamadas_ia` — así que centralizarlos aquí evita repetir (y
eventualmente desincronizar) esa lógica tres veces. Cada `agents/*.py`
sigue siendo el único que sabe qué prompt, qué modelo y qué schema le
corresponden.

El Agente 1 (Entrevistador, diferido a Paso 10) no usa esto: es
conversacional multi-turno, no un intercambio JSON-in/JSON-out de una sola
llamada.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.ai.client import call_agent, parse_agent_json
from app.ai.models import calculate_cost_usd
from app.config import Settings
from app.repositories.ai_calls import LlamadaIARow, save_llamada_ia

logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

_RATIO_ALERTA_CERCA_DEL_LIMITE = 0.85
"""Detección temprana (sección 24 del plan: nunca un fallo silencioso).
`call_agent` ya detecta y aborta cuando una respuesta SE CORTA por
max_tokens (`AgentResponseError`) — pero para entonces ya se perdió esa
llamada y, si era tarde en el ciclo, el costo de las llamadas previas de
ese símbolo. Esto es distinto: si una llamada que SÍ terminó bien ya usó
el 85% o más de su `max_tokens`, es señal de que el margen se está
agotando, antes de que deje de alcanzar — igual que pasó primero con el
Consolidador (`MAX_TOKENS_CONSOLIDADOR`) y después con el Analista de
eventos (`MAX_TOKENS_ANALISTA_EVENTOS`), ambos ajustados recién DESPUÉS de
fallar en una corrida real. No aborta nada ni cambia el resultado — solo
deja rastro en el log para poder subir el límite de ese agente antes del
próximo corte, y para ir acumulando datos reales de uso por agente en vez
de seguir ajustando `max_tokens` a ojo."""


class AgentValidationError(Exception):
    """El JSON del modelo parseó correctamente, pero no cumple el schema
    esperado del agente (campo faltante, valor fuera de las opciones
    permitidas, un campo extra no pedido, etc.).

    Se distingue de `AgentResponseError` (JSON inválido a secas) porque acá
    sí sabemos la forma exacta que se esperaba — útil para diagnosticar si
    el prompt necesita ajustarse.
    """


@dataclass(frozen=True)
class AgentRunResult:
    """Resultado de correr un agente: su salida ya validada, más el id del
    registro de auditoría en `llamadas_ia`."""

    output: BaseModel
    llamada_id: uuid.UUID
    input_tokens: int
    output_tokens: int
    cost_usd: object  # Decimal — evitar el import solo para el tipo aquí


def run_json_agent(
    session: Session,
    settings: Settings,
    *,
    agente: str,
    modelo: str,
    prompt_version: str,
    system_prompt: str,
    datos: dict,
    schema: type[SchemaT],
    ciclo_id: uuid.UUID | None = None,
    extra_validate: Callable[[SchemaT], None] | None = None,
    max_tokens: int = 4096,
) -> AgentRunResult:
    """Llama al modelo, valida su respuesta contra `schema` y persiste el
    registro de auditoría. Propaga `AgentResponseError` (JSON inválido) o
    `AgentValidationError` (JSON válido pero con forma incorrecta) en vez
    de devolver un resultado parcial — quien orquesta el ciclo decide qué
    hacer ante una interpretación fallida (sección P3 del plan: nunca
    sustituir un dato faltante por un supuesto).

    `extra_validate`, si se pasa, corre DESPUÉS de la validación de
    schema y ANTES de persistir — para chequeos que Pydantic no puede
    hacer solo (por ejemplo, que un id referenciado exista realmente en la
    base de datos). Debe lanzar `AgentValidationError` si algo no es
    válido; una respuesta que falla aquí tampoco se persiste, igual que
    una que falla el schema.

    `max_tokens` por defecto (4096) alcanza de sobra a los agentes que
    analizan UN símbolo por llamada (2, 3, 4). El Consolidador, cuya
    salida crece con la cantidad de candidatos que recibe, pasa un valor
    mayor explícitamente — ver `app.ai.agents.consolidator` (bug real,
    2026-09-30: con varios candidatos la respuesta se cortaba a mitad de
    JSON con el valor por defecto)."""
    user_content = json.dumps(datos, ensure_ascii=False, default=str)

    call_result = call_agent(
        settings,
        model=modelo,
        system_prompt=system_prompt,
        user_content=user_content,
        max_tokens=max_tokens,
    )

    if call_result.output_tokens >= max_tokens * _RATIO_ALERTA_CERCA_DEL_LIMITE:
        logger.warning(
            "El agente '%s' usó %d de %d tokens de salida permitidos (%.0f%%) en esta "
            "llamada — terminó bien, pero cerca del límite. Considerar subir su "
            "max_tokens antes de que la próxima corrida lo corte a mitad de JSON.",
            agente,
            call_result.output_tokens,
            max_tokens,
            100 * call_result.output_tokens / max_tokens,
            extra={
                "ciclo_id": str(ciclo_id) if ciclo_id else None,
                "agente": agente,
                "modelo": modelo,
                "output_tokens": call_result.output_tokens,
                "max_tokens": max_tokens,
            },
        )

    parsed = parse_agent_json(call_result.raw_text)

    try:
        output = schema.model_validate(parsed)
    except ValidationError as exc:
        raise AgentValidationError(
            f"La respuesta de '{agente}' no cumple el schema {schema.__name__}: {exc}"
        ) from exc

    if extra_validate is not None:
        extra_validate(output)

    cost_usd = calculate_cost_usd(
        modelo,
        call_result.input_tokens,
        call_result.output_tokens,
        cache_creation_input_tokens=call_result.cache_creation_input_tokens,
        cache_read_input_tokens=call_result.cache_read_input_tokens,
    )

    llamada_id = save_llamada_ia(
        session,
        LlamadaIARow(
            agente=agente,
            modelo=modelo,
            prompt_version=prompt_version,
            input_tokens=call_result.input_tokens,
            output_tokens=call_result.output_tokens,
            cost_usd=cost_usd,
            respuesta=parsed,
            ciclo_id=ciclo_id,
        ),
    )

    return AgentRunResult(
        output=output,
        llamada_id=llamada_id,
        input_tokens=call_result.input_tokens,
        output_tokens=call_result.output_tokens,
        cost_usd=cost_usd,
    )
