"""Agente 1 — Entrevistador (sección 7.1/7.2 del plan; Paso 10b).

Conversacional multi-turno: en cada turno recibe la conversación completa y
responde con UNA pregunta (texto) o, al terminar, con el objeto JSON del
perfil. Por eso no usa `run_json_agent` (que es de un solo intercambio
JSON-in/JSON-out).

Garantías (P1/P2/P4):

- Sin herramientas y sin acceso a nada más que texto: no importa nada de
  `app.execution` ni `app.data`.
- Su resultado es INFORMATIVO: este módulo no lee ni escribe perfiles. Quien
  lo llama (`app/services/interview.py`) los guarda, y ningún otro módulo del
  sistema los lee para decidir (lo verifica `tests/unit/test_architecture.py`).
- El máximo de 8 preguntas lo impone el código: tras la octava respuesta se
  pide el JSON final con un mensaje de cierre.
- Cada llamada (exitosa o fallida) queda en `llamadas_ia` con su costo real
  (control de gasto de la sección 11 del plan). NO se guardan las respuestas
  del usuario como texto: solo el perfil final y las preguntas del agente.

Cómo se distingue una pregunta del perfil final: el prompt exige que el
perfil sea "únicamente un objeto JSON, sin texto adicional"; si la respuesta
empieza con `{` (o con una cerca de código) se trata como perfil y se valida
contra `PerfilUsuarioOutput`. Cualquier otro texto es una pregunta.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.ai.client import AgentCallResult, AgentResponseError, call_chat, parse_agent_json
from app.ai.models import MODEL_ENTREVISTADOR, calculate_cost_usd
from app.ai.prompts.interviewer import (
    MENSAJE_DE_CIERRE,
    MENSAJE_DE_INICIO,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
)
from app.ai.schemas import PerfilUsuarioOutput
from app.config import Settings
from app.repositories.ai_calls import LlamadaIARow, save_llamada_ia

logger = logging.getLogger(__name__)

AGENTE = "entrevistador"
MAX_PREGUNTAS = 8
MAX_TOKENS_ENTREVISTADOR = 2048
MAX_CARACTERES_RESPUESTA_USUARIO = 2000
"""Tope de una respuesta del usuario (control de costo y de abuso del
contexto). Más largo se rechaza en el servicio, no se recorta en silencio."""

Rol = Literal["usuario", "agente"]


class EntrevistaError(Exception):
    """La llamada falló o el agente no respetó el formato; el mensaje es apto
    para mostrarse al usuario. La llamada ya quedó registrada en `llamadas_ia`."""


@dataclass(frozen=True)
class Mensaje:
    rol: Rol
    texto: str


@dataclass(frozen=True)
class TurnoEntrevistador:
    tipo: Literal["pregunta", "perfil"]
    texto: str | None
    perfil: PerfilUsuarioOutput | None
    preguntas_hechas: int
    """Cuántas preguntas lleva el agente (incluida esta si `tipo == "pregunta"`)."""
    modelo: str = MODEL_ENTREVISTADOR
    prompt_version: str = PROMPT_VERSION


def construir_mensajes(historial: list[Mensaje]) -> list[dict[str, str]]:
    """Conversación para la API: empieza con el mensaje de inicio (mecánico) y
    alterna agente/usuario. Con 8 preguntas ya hechas añade el cierre."""
    if historial and historial[-1].rol != "usuario":
        raise ValueError("El historial debe terminar en una respuesta del usuario (o estar vacío).")
    mensajes = [{"role": "user", "content": MENSAJE_DE_INICIO}]
    for m in historial:
        mensajes.append({"role": "assistant" if m.rol == "agente" else "user", "content": m.texto})
    if sum(1 for m in historial if m.rol == "agente") >= MAX_PREGUNTAS:
        mensajes.append({"role": "user", "content": MENSAJE_DE_CIERRE})
    return mensajes


def _parece_perfil(texto: str) -> bool:
    t = texto.lstrip()
    return t.startswith("{") or t.startswith("```")


def _registrar(
    session: Session, *, resultado: AgentCallResult, respuesta: dict | None, exitosa: bool, error: str | None
) -> None:
    save_llamada_ia(
        session,
        LlamadaIARow(
            agente=AGENTE,
            modelo=MODEL_ENTREVISTADOR,
            prompt_version=PROMPT_VERSION,
            input_tokens=resultado.input_tokens,
            output_tokens=resultado.output_tokens,
            cost_usd=calculate_cost_usd(
                MODEL_ENTREVISTADOR,
                resultado.input_tokens,
                resultado.output_tokens,
                cache_creation_input_tokens=resultado.cache_creation_input_tokens,
                cache_read_input_tokens=resultado.cache_read_input_tokens,
            ),
            respuesta=respuesta,
            exitosa=exitosa,
            error_message=error,
        ),
    )


def run_interviewer_turn(session: Session, settings: Settings, historial: list[Mensaje]) -> TurnoEntrevistador:
    """Siguiente turno del Entrevistador para `historial` (vacío = primera pregunta)."""
    hechas = sum(1 for m in historial if m.rol == "agente")
    try:
        resultado = call_chat(
            settings,
            model=MODEL_ENTREVISTADOR,
            system_prompt=SYSTEM_PROMPT,
            messages=construir_mensajes(historial),
            max_tokens=MAX_TOKENS_ENTREVISTADOR,
        )
    except AgentResponseError as exc:  # respuesta cortada por max_tokens: la API la cobró igual
        if exc.tokens is not None:
            _registrar(session, resultado=exc.tokens, respuesta=None, exitosa=False, error=str(exc)[:500])
        raise EntrevistaError("La respuesta del entrevistador se cortó. Inténtalo de nuevo.") from exc

    texto = resultado.raw_text.strip()
    if not texto:
        _registrar(session, resultado=resultado, respuesta=None, exitosa=False, error="respuesta vacía")
        raise EntrevistaError("El entrevistador respondió vacío. Inténtalo de nuevo.")

    if _parece_perfil(texto):
        try:
            perfil = PerfilUsuarioOutput.model_validate(parse_agent_json(texto))
        except (AgentResponseError, ValidationError) as exc:
            _registrar(session, resultado=resultado, respuesta=None, exitosa=False, error=str(exc)[:500])
            raise EntrevistaError(
                "El perfil que entregó el entrevistador no cumple el formato esperado. Inténtalo de nuevo."
            ) from exc
        _registrar(session, resultado=resultado, respuesta=perfil.model_dump(), exitosa=True, error=None)
        return TurnoEntrevistador("perfil", None, perfil, hechas)

    if hechas >= MAX_PREGUNTAS:
        # Ya se pidió el cierre y aun así llegó otra pregunta: no se sigue preguntando.
        _registrar(session, resultado=resultado, respuesta=None, exitosa=False, error="pregunta tras el cierre")
        raise EntrevistaError(
            "El entrevistador no cerró el perfil tras las 8 preguntas. Inténtalo de nuevo o reinicia la entrevista."
        )

    _registrar(session, resultado=resultado, respuesta={"tipo": "pregunta", "texto": texto}, exitosa=True, error=None)
    return TurnoEntrevistador("pregunta", texto, None, hechas + 1)
