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
import logging
from dataclasses import dataclass

import anthropic

from app.config import Settings

logger = logging.getLogger(__name__)


class AgentResponseError(Exception):
    """El modelo respondió, pero el texto no es JSON válido — o la
    respuesta se cortó antes de terminar (ver `call_agent` sobre
    `stop_reason == "max_tokens"`).

    Deliberadamente NO se intenta "reparar" ni rellenar con un valor por
    defecto (sección P3 del plan: cero datos inventados) — quien llama debe
    tratar esto como una interpretación fallida, no como un resultado
    parcial utilizable.

    `tokens` (default `None`): cuando el corte es por `max_tokens`, la API
    SÍ respondió y SÍ se cobra esa llamada — Anthropic no la regala solo
    porque el JSON quedó incompleto. Antes de este campo, `run_json_agent`
    no tenía forma de conocer esos tokens para registrar el costo real de
    una llamada fallida (bug real 2026-10-02: ver
    `app/db/models/ai_calls.py::LlamadaIA.exitosa`) — se pierde esa
    información apenas termina `call_agent` si no viaja con la excepción.
    Queda en `None` para el caso de JSON inválido detectado en
    `parse_agent_json` (no tiene acceso a la respuesta cruda del modelo, ni
    falta le hace: quien la llama ya tiene el `AgentCallResult` completo en
    ese punto)."""

    def __init__(self, message: str, *, tokens: AgentCallResult | None = None) -> None:
        super().__init__(message)
        self.tokens = tokens


@dataclass(frozen=True)
class AgentCallResult:
    """Resultado crudo de una llamada al modelo, antes de validar el JSON
    contra el schema del agente.

    `cache_creation_input_tokens`/`cache_read_input_tokens` (default 0,
    para no romper construcciones existentes en tests que no los pasan):
    BUG REAL (2026-10-02): el prompt de sistema de cada agente se manda con
    `cache_control: ephemeral` (sección 4.3 del plan), pero la API de
    Anthropic reporta esos tokens en DOS campos separados de `usage`
    (`cache_creation_input_tokens` al escribir el cache,
    `cache_read_input_tokens` al reutilizarlo) — `input_tokens` NUNCA los
    incluye, solo cuenta lo que queda después del último "cache breakpoint"
    (típicamente el bloque DATOS de esa llamada, chico). Como
    `calculate_cost_usd` solo multiplicaba `input_tokens`, el costo de
    escribir/leer el prompt de sistema cacheado — la parte más grande de
    cada llamada — no se estaba cobrando NUNCA en `cost_usd`, subestimando
    el costo real de forma sistemática en todas las llamadas, no solo en
    casos raros. Detectado porque el saldo real de la cuenta de Anthropic
    bajaba más rápido que el costo que este sistema venía reportando."""

    raw_text: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class LlaveIARechazada(Exception):
    """Anthropic rechazó la llave de API (401 o 403: vencida, revocada, inválida o sin
    permiso). El mensaje nunca incluye la llave."""


def verificar_llave(settings: Settings, *, model: str) -> None:
    """Llamada MÍNIMA (1 token de salida, ~10 de entrada: una fracción de centavo) para
    saber si la llave sirve ANTES de gastar minutos de backfill e indicadores en un ciclo.

    - Llave aceptada: devuelve `None`.
    - 401/403: lanza `LlaveIARechazada`.
    - Cualquier otro fallo (red, 429, 5xx, saturación) se propaga tal cual: no prueba que
      la llave esté mal, y quien llama decide. Sin reintentos y con tiempo límite corto.

    No se registra en `llamadas_ia` (el costo es despreciable y no es un agente)."""
    if not settings.anthropic_api_key:
        raise LlaveIARechazada("ANTHROPIC_API_KEY no está configurada en el .env")
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=0, timeout=30.0)
    try:
        client.messages.create(model=model, max_tokens=1, messages=[{"role": "user", "content": "ok"}])
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        solicitud = getattr(exc, "request_id", None)
        raise LlaveIARechazada(
            f"Anthropic rechazó la llave (HTTP {exc.status_code}"
            + (f", solicitud {solicitud}" if solicitud else "")
            + ")"
        ) from None


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
    return _enviar(
        settings,
        model=model,
        system_prompt=system_prompt,
        messages=[{"role": "user", "content": user_content}],
        max_tokens=max_tokens,
    )


def call_chat(
    settings: Settings,
    *,
    model: str,
    system_prompt: str,
    messages: list[dict[str, str]],
    max_tokens: int = 2048,
) -> AgentCallResult:
    """Igual que `call_agent` pero con una conversación de varios turnos
    (`messages`: lista de `{"role": "user"|"assistant", "content": str}`,
    empezando y terminando en `user`). Solo la usa el Entrevistador (Agente 1,
    Paso 10b): sigue sin herramientas ni acceso a nada más que texto (P4)."""
    return _enviar(settings, model=model, system_prompt=system_prompt, messages=messages, max_tokens=max_tokens)


def _enviar(
    settings: Settings,
    *,
    model: str,
    system_prompt: str,
    messages: list[dict[str, str]],
    max_tokens: int,
) -> AgentCallResult:
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
        messages=messages,
    )

    raw_text = "".join(block.text for block in response.content if block.type == "text")

    call_result = AgentCallResult(
        raw_text=raw_text,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cache_creation_input_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
    )

    if response.stop_reason == "max_tokens":
        # BUG REAL encontrado corriendo scripts/run_daily_cycle.py contra la
        # API real (2026-09-30): con varios candidatos, la respuesta del
        # Consolidador puede necesitar más de max_tokens=4096 y el modelo la
        # corta a mitad de una cadena — json.JSONDecoder la reporta como
        # "Unterminated string", un mensaje que no deja claro que la causa
        # real fue quedarse sin espacio, no un JSON mal formado. Se detecta
        # aquí, ANTES de intentar parsear, para que el error sea diagnosticable
        # de inmediato (sección 24 del plan: contexto suficiente para
        # diagnosticar) — nunca se intenta "cerrar" el JSON truncado a mano,
        # eso sería inventar el resto de la respuesta (P3 del plan).
        #
        # `tokens=call_result` (bug real 2026-10-02): esta llamada SÍ la
        # cobra Anthropic, aunque el JSON haya quedado incompleto — sin
        # adjuntar los tokens reales acá, quien llama (`run_json_agent`) no
        # tiene forma de registrar ese costo real en `llamadas_ia`.
        raise AgentResponseError(
            f"La respuesta de '{model}' se cortó por alcanzar max_tokens={max_tokens} "
            f"({response.usage.output_tokens} tokens de salida generados) antes de "
            "terminar el JSON. Quien llama debe pasar un max_tokens mayor para este "
            "agente — nunca se completa el JSON a mano.\n"
            f"Texto recibido (últimos 300 caracteres): {raw_text[-300:]!r}",
            tokens=call_result,
        )

    return call_result


def parse_agent_json(raw_text: str) -> dict:
    """Extrae y parsea el JSON de la respuesta de un agente.

    Los prompts piden explícitamente "Solo JSON, sin texto adicional", pero
    los modelos a veces no lo respetan al pie de la letra. Se toleran DOS
    envoltorios superficiales, nunca un intento de "adivinar" JSON a partir
    de texto libre:

    1. Una cerca de código (```json ... ```) alrededor de todo el bloque.
    2. Texto adicional DESPUÉS del objeto JSON ya completo (visto en la
       práctica: Haiku 4.5 respondiendo al Auditor con el JSON completo y
       luego una nota aparte). Se parsea solo el primer valor JSON válido
       (`json.JSONDecoder.raw_decode`, no una expresión regular) y el resto
       se descarta — pero se registra en el log, nunca en silencio, porque
       indica que el modelo no está siguiendo el prompt y conviene poder
       verlo si se repite.

    Cualquier otra cosa (JSON incompleto, texto libre sin estructura) sigue
    siendo un `AgentResponseError`, no un valor de relleno (sección P3 del
    plan). El error incluye un fragmento de la respuesta cruda para poder
    diagnosticarlo sin tener que reproducir la llamada (y sin haber perdido
    ya el crédito de esa llamada, que si el JSON no parsea nunca llega a
    persistirse en `llamadas_ia` — ver `app/ai/agents/_runner.py`).
    """
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[len("json") :]
        text = text.strip()

    try:
        parsed, end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as exc:
        raise AgentResponseError(
            f"La respuesta del agente no es JSON válido: {exc}\n"
            f"Texto recibido (primeros 500 caracteres): {raw_text[:500]!r}"
        ) from exc

    resto = text[end:].strip()
    if resto:
        logger.warning(
            "El agente devolvió texto adicional después del JSON; se ignoró "
            "(el prompt pide 'solo JSON, sin texto adicional'): %r",
            resto[:500],
        )

    if not isinstance(parsed, dict):
        raise AgentResponseError(
            f"La respuesta del agente debía ser un objeto JSON, llegó {type(parsed).__name__}."
        )
    return parsed
