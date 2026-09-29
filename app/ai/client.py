"""Cliente delgado sobre el SDK oficial de Anthropic (sección 5 del plan:
"IA: SDK oficial de Anthropic — soporte nativo de prompt caching y salida
estructurada").

Separación deliberada entre el prompt de sistema (instrucciones fijas: rol,
prohibiciones, qué evaluar, formato de salida — siempre el mismo texto para
un agente dado) y el mensaje de usuario (el bloque DATOS, que cambia en
cada llamada): esto es lo que permite marcar el prompt de sistema con
`cache_control` y aprovechar el prompt caching que menciona la sección 4.3
del plan ("Prompt caching sobre el contexto fijo") sin cachear —ni
reenviar como si fuera fijo— el JSON variable de cada símbolo.

Este módulo NO calcula nada (P1 del plan) y no tiene acceso a ninguna
herramienta de ejecución (P2/P4): solo envía texto y recibe texto. No
importa nada de `app.execution` ni `app.data.exchange_client`, y no valida
el JSON de salida contra ningún schema — eso lo hace quien llama
(`app/ai/agents/*.py`), que sí sabe contra qué `app.ai.schemas` validar su
propia respuesta.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import anthropic

from app.config import Settings


class AgentResponseError(Exception):
    """El modelo respondió, pero el texto no es JSON válido.

    Deliberadamente NO se intenta "reparar" ni rellenar con un valor por
    defecto (sección P3 del plan: cero datos inventados) — quien llama debe
    tratar esto como una interpretación fallida, no como un resultado
    parcial utilizable.
    """


@dataclass(frozen=True)
class AgentCallResult:
    """Resultado crudo de una llamada al modelo, antes de validar el JSON
    contra el schema del agente."""

    raw_text: str
    input_tokens: int
    output_tokens: int


def call_agent(
    settings: Settings,
    *,
    model: str,
    system_prompt: str,
    user_content: str,
    max_tokens: int = 4096,
) -> AgentCallResult:
    """Envía `system_prompt` (cacheable) y `user_content` (el bloque DATOS,
    sin cachear) y devuelve el texto crudo de la respuesta, sin validar."""
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY no está configurada. Ver .env.example — esta "
            "clave vive únicamente en el .env del proyecto (sección 11 del "
            "plan), nunca como variable de entorno del sistema."
        )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
    )

    raw_text = "".join(block.text for block in response.content if block.type == "text")

    return AgentCallResult(
        raw_text=raw_text,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


def parse_agent_json(raw_text: str) -> dict:
    """Extrae y parsea el JSON de la respuesta de un agente.

    Los prompts piden explícitamente "Solo JSON, sin texto adicional", pero
    los modelos a veces envuelven la respuesta en una cerca de código
    (```json ... ```) de todas formas — se tolera ÚNICAMENTE ese envoltorio
    superficial, nunca un intento de "adivinar" JSON a partir de texto
    libre. Cualquier otra cosa es un `AgentResponseError`, no un valor de
    relleno (sección P3 del plan).
    """
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[len("json") :]
        text = text.strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentResponseError(f"La respuesta del agente no es JSON válido: {exc}") from exc

    if not isinstance(parsed, dict):
        raise AgentResponseError(
            f"La respuesta del agente debía ser un objeto JSON, llegó {type(parsed).__name__}."
        )
    return parsed
