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

SchemaT = TypeVar("SchemaT", bound=BaseModel)


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
    una que falla el schema."""
    user_content = json.dumps(datos, ensure_ascii=False, default=str)

    call_result = call_agent(
        settings,
        model=modelo,
        system_prompt=system_prompt,
        user_content=user_content,
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

    cost_usd = calculate_cost_usd(modelo, call_result.input_tokens, call_result.output_tokens)

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
