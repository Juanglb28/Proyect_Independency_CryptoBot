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
from decimal import Decimal
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.ai.client import AgentCallResult, AgentResponseError, call_agent, parse_agent_json
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


MAX_REINTENTOS_POR_FALLO_DE_SCHEMA = 1
"""Cuántas veces se REPITE una llamada cuya respuesta no cumplió el schema
del agente, antes de abortar (decisión del usuario, 2026-10-04). Bug real
(2026-10-04): el Analista de eventos agregó un campo `fecha` que su schema
estricto (`extra="forbid"`) no permite, y el ciclo entero abortó —
perdiendo el trabajo ya pagado de los demás candidatos — aunque las dos
corridas anteriores, con el mismo prompt, habían pasado. Reintentar UNA
vez cuesta una llamada extra solo cuando hay falla.

Es un parámetro de costo fijo en código, no una variable de entorno
(sección 10 del master prompt). Solo aplica a `AgentSchemaError`: una
respuesta cortada por `max_tokens` o con JSON inválido no se reintenta (la
primera se repetiría igual y la segunda no es un problema de schema), ni
tampoco un fallo de `extra_validate` (p. ej. el Consolidador citando una
fuente que no existe en el catálogo: es una señal de integridad de datos,
no ruido de formato, y reintentar esa decisión es una pregunta distinta)."""


class AgentValidationError(Exception):
    """El JSON del modelo parseó correctamente, pero no cumple el schema
    esperado del agente (campo faltante, valor fuera de las opciones
    permitidas, un campo extra no pedido, etc.).

    Se distingue de `AgentResponseError` (JSON inválido a secas) porque acá
    sí sabemos la forma exacta que se esperaba — útil para diagnosticar si
    el prompt necesita ajustarse.
    """


class AgentSchemaError(AgentValidationError):
    """Subtipo de `AgentValidationError`: el JSON parseó bien pero no cumple
    el schema Pydantic del agente. Es el ÚNICO fallo que `run_json_agent`
    reintenta (ver `MAX_REINTENTOS_POR_FALLO_DE_SCHEMA`). Sigue siendo un
    `AgentValidationError`, así que quien ya captura ese tipo no cambia."""


def _persistir_llamada_fallida(
    session: Session,
    *,
    agente: str,
    modelo: str,
    prompt_version: str,
    ciclo_id: uuid.UUID | None,
    call_result: AgentCallResult | None,
    exc: Exception,
) -> tuple[int, int, Decimal] | None:
    """Registra en `llamadas_ia` el costo real de una llamada que falló
    DESPUÉS de que la API respondiera (JSON inválido, truncada por
    `max_tokens`, o con forma distinta al schema del agente) — Anthropic la
    cobra igual (bug real 2026-10-02, ver
    `app/db/models/ai_calls.py::LlamadaIA`).

    Los tokens reales vienen de dos fuentes posibles, nunca inventados
    (P3 del plan): `exc.tokens` (cuando `call_agent` detectó el corte por
    `max_tokens` ANTES de devolver un `AgentCallResult` — ver
    `AgentResponseError.tokens`) o `call_result` (cuando `call_agent` sí
    devolvió, y la falla fue después: JSON inválido o schema incorrecto).
    Si ninguna de las dos está disponible (un error antes de recibir
    cualquier respuesta de la API, que normalmente tampoco cobra Anthropic),
    no se persiste nada — no hay tokens reales que registrar.

    Devuelve `(input_tokens, output_tokens, cost_usd)` de lo que se cobró, o
    `None` si no hubo respuesta que cobrar — `run_json_agent` lo necesita
    para sumar el costo de un intento fallido al total del paso cuando
    reintenta."""
    tokens = getattr(exc, "tokens", None) or call_result
    if tokens is None:
        return None

    cost_usd = calculate_cost_usd(
        modelo,
        tokens.input_tokens,
        tokens.output_tokens,
        cache_creation_input_tokens=tokens.cache_creation_input_tokens,
        cache_read_input_tokens=tokens.cache_read_input_tokens,
    )
    save_llamada_ia(
        session,
        LlamadaIARow(
            agente=agente,
            modelo=modelo,
            prompt_version=prompt_version,
            input_tokens=tokens.input_tokens,
            output_tokens=tokens.output_tokens,
            cost_usd=cost_usd,
            respuesta=None,
            exitosa=False,
            error_message=str(exc),
            ciclo_id=ciclo_id,
        ),
    )
    return tokens.input_tokens, tokens.output_tokens, cost_usd


@dataclass(frozen=True)
class AgentRunResult:
    """Resultado de correr un agente: su salida ya validada, más el id del
    registro de auditoría en `llamadas_ia`."""

    output: BaseModel
    llamada_id: uuid.UUID
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    intentos: int = 1
    """Cantidad de llamadas que costó este paso (1 si salió a la primera).
    `input_tokens`/`output_tokens`/`cost_usd` son el TOTAL de todos los
    intentos — lo que realmente se facturó por este paso — no solo los del
    intento exitoso; `llamada_id` es el del intento exitoso."""


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
    JSON con el valor por defecto).

    Una interpretación fallida (`AgentResponseError`/`AgentValidationError`)
    igual deja un registro de costo en `llamadas_ia` antes de propagarse —
    ver `_persistir_llamada_fallida` (bug real 2026-10-02): Anthropic cobra
    esa llamada aunque el resultado no sirva, así que debe quedar
    registrada, sin que esto cambie en nada el comportamiento de quien
    llama (la excepción se relanza igual)."""
    user_content = json.dumps(datos, ensure_ascii=False, default=str)

    # Lo que costaron los intentos FALLIDOS de este paso (se suma al del
    # intento exitoso, para que el costo del ciclo refleje lo facturado).
    input_fallidos = 0
    output_fallidos = 0
    costo_fallidos = Decimal(0)

    for intento in range(1, MAX_REINTENTOS_POR_FALLO_DE_SCHEMA + 2):
        call_result: AgentCallResult | None = None
        try:
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
                raise AgentSchemaError(
                    f"La respuesta de '{agente}' no cumple el schema {schema.__name__}: {exc}"
                ) from exc

            if extra_validate is not None:
                extra_validate(output)
        except (AgentResponseError, AgentValidationError) as exc:
            cobrado = _persistir_llamada_fallida(
                session,
                agente=agente,
                modelo=modelo,
                prompt_version=prompt_version,
                ciclo_id=ciclo_id,
                call_result=call_result,
                exc=exc,
            )
            if cobrado is not None:
                input_fallidos += cobrado[0]
                output_fallidos += cobrado[1]
                costo_fallidos += cobrado[2]

            if isinstance(exc, AgentSchemaError) and intento <= MAX_REINTENTOS_POR_FALLO_DE_SCHEMA:
                logger.warning(
                    "La respuesta del agente '%s' no cumplió su schema (intento %d de %d); "
                    "se reintenta la llamada. Error: %s",
                    agente,
                    intento,
                    MAX_REINTENTOS_POR_FALLO_DE_SCHEMA + 1,
                    str(exc)[:300],
                    extra={
                        "ciclo_id": str(ciclo_id) if ciclo_id else None,
                        "agente": agente,
                        "modelo": modelo,
                        "intento": intento,
                    },
                )
                continue
            raise
        break

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
        input_tokens=call_result.input_tokens + input_fallidos,
        output_tokens=call_result.output_tokens + output_fallidos,
        cost_usd=cost_usd + costo_fallidos,
        intentos=intento,
    )
