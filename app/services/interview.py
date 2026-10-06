"""Entrevista del Agente 1 desde la interfaz (Paso 10b).

Es, junto con `commands`, la otra parte de `app/services` que escribe, y el
ÚNICO servicio que puede llamar a un agente de IA (excepción por archivo en
`tests/unit/test_architecture.py`). Reglas:

- El perfil es INFORMATIVO (decisión del usuario, 2026-10-06): este módulo lo
  guarda y la página lo muestra; nada más en el sistema lo lee.
- Cada turno hace una llamada al Entrevistador, que queda en `llamadas_ia`
  con su costo aunque falle. La conversación vive en la sesión del navegador
  (no es estado operativo): si se recarga la página, se empieza de nuevo.
- Las respuestas del usuario no se guardan como texto; solo el perfil final,
  y solo cuando el usuario lo confirma.
- Errores: el usuario recibe un mensaje claro; el detalle técnico va al log
  (sin secretos).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.ai.agents.interviewer import (
    MAX_CARACTERES_RESPUESTA_USUARIO,
    MAX_PREGUNTAS,
    EntrevistaError,
    Mensaje,
    run_interviewer_turn,
)
from app.db.session import session_scope
from app.repositories.perfil_usuario import PerfilRow, save_perfil
from app.services._escritura import ResultadoAccion, escritura_factory
from app.services.session import settings

__all__ = [
    "MAX_CARACTERES_RESPUESTA_USUARIO", "MAX_PREGUNTAS", "Mensaje", "PerfilPropuesto", "ResultadoTurno",
    "guardar_perfil", "siguiente_turno",
]

logger = logging.getLogger(__name__)
_COMPONENT = "services.interview"
_ERROR_GENERICO = "No se pudo completar el paso de la entrevista. Revisa los registros e inténtalo de nuevo."


@dataclass(frozen=True)
class PerfilPropuesto:
    """Perfil que entregó el agente, antes de que el usuario lo confirme."""

    horizonte_meses: int
    perdida_maxima_pct: Decimal
    experiencia: str
    activos_excluidos: tuple[str, ...]
    tolerancia_declarada: str
    objetivo_rentabilidad_pct: Decimal
    objetivo_es_realista: bool
    disponibilidad_aprobacion: str
    pct_patrimonio: Decimal
    contradicciones_detectadas: tuple[str, ...]
    confianza_perfil: str
    modelo: str
    prompt_version: str


@dataclass(frozen=True)
class ResultadoTurno:
    ok: bool
    tipo: str | None  # "pregunta" | "perfil" | None si falló
    texto: str | None
    perfil: PerfilPropuesto | None
    preguntas_hechas: int
    mensaje: str


@contextmanager
def _abrir_sesion() -> Iterator[Session]:
    with session_scope(escritura_factory()) as session:
        yield session


def _dos_decimales(valor: float) -> Decimal:
    return Decimal(str(valor)).quantize(Decimal("0.01"))


def _fallo(mensaje: str, hechas: int) -> ResultadoTurno:
    return ResultadoTurno(False, None, None, None, hechas, mensaje)


def siguiente_turno(historial: tuple[Mensaje, ...]) -> ResultadoTurno:
    """Siguiente turno de la entrevista para `historial` (vacío = empezar)."""
    hechas = sum(1 for m in historial if m.rol == "agente")
    if historial and historial[-1].rol == "usuario":
        respuesta = historial[-1].texto.strip()
        if not respuesta:
            return _fallo("Escribe una respuesta antes de continuar.", hechas)
        if len(respuesta) > MAX_CARACTERES_RESPUESTA_USUARIO:
            return _fallo(f"La respuesta es demasiado larga (máximo {MAX_CARACTERES_RESPUESTA_USUARIO} caracteres).", hechas)
    cfg = settings()
    if not cfg.anthropic_api_key:
        return _fallo("Falta ANTHROPIC_API_KEY en el archivo .env: la entrevista necesita el modelo.", hechas)

    try:
        with _abrir_sesion() as session:
            # La llamada fallida también se registra (cobro real): por eso el
            # error del agente se captura DENTRO de la transacción, para que se confirme.
            try:
                turno = run_interviewer_turn(session, cfg, list(historial))
            except EntrevistaError as exc:
                return _fallo(str(exc), hechas)
    except Exception:  # noqa: BLE001 — red, API, base: mensaje claro al usuario, detalle al log
        logger.exception("entrevista fallida", extra={"component": _COMPONENT})
        return _fallo(_ERROR_GENERICO, hechas)

    logger.info(
        "entrevista: turno", extra={"component": _COMPONENT, "tipo": turno.tipo, "preguntas": turno.preguntas_hechas}
    )
    if turno.tipo == "pregunta":
        return ResultadoTurno(True, "pregunta", turno.texto, None, turno.preguntas_hechas, "")
    p = turno.perfil
    return ResultadoTurno(
        True, "perfil", None,
        PerfilPropuesto(
            horizonte_meses=p.horizonte_meses,
            perdida_maxima_pct=_dos_decimales(p.perdida_maxima_pct),
            experiencia=p.experiencia,
            activos_excluidos=tuple(p.activos_excluidos),
            tolerancia_declarada=p.tolerancia_declarada,
            objetivo_rentabilidad_pct=_dos_decimales(p.objetivo_rentabilidad_pct),
            objetivo_es_realista=p.objetivo_es_realista,
            disponibilidad_aprobacion=p.disponibilidad_aprobacion,
            pct_patrimonio=_dos_decimales(p.pct_patrimonio),
            contradicciones_detectadas=tuple(p.contradicciones_detectadas),
            confianza_perfil=p.confianza_perfil,
            modelo=turno.modelo,
            prompt_version=turno.prompt_version,
        ),
        turno.preguntas_hechas, "",
    )


def guardar_perfil(perfil: PerfilPropuesto) -> ResultadoAccion:
    """Guarda el perfil confirmado por el usuario como una fila nueva (el anterior queda como historia)."""
    try:
        with _abrir_sesion() as session:
            save_perfil(
                session,
                PerfilRow(
                    horizonte_meses=perfil.horizonte_meses,
                    perdida_maxima_pct=perfil.perdida_maxima_pct,
                    experiencia=perfil.experiencia,
                    activos_excluidos=list(perfil.activos_excluidos),
                    tolerancia_declarada=perfil.tolerancia_declarada,
                    objetivo_rentabilidad_pct=perfil.objetivo_rentabilidad_pct,
                    objetivo_es_realista=perfil.objetivo_es_realista,
                    disponibilidad_aprobacion=perfil.disponibilidad_aprobacion,
                    pct_patrimonio=perfil.pct_patrimonio,
                    contradicciones_detectadas=list(perfil.contradicciones_detectadas),
                    confianza_perfil=perfil.confianza_perfil,
                    modelo=perfil.modelo,
                    prompt_version=perfil.prompt_version,
                ),
            )
    except Exception:  # noqa: BLE001
        logger.exception("guardar perfil fallido", extra={"component": _COMPONENT})
        return ResultadoAccion(False, "ERROR", "No se pudo guardar el perfil y no se cambió nada. Inténtalo de nuevo.")
    logger.info("perfil guardado", extra={"component": _COMPONENT})
    return ResultadoAccion(True, "GUARDADO", "Perfil guardado. Es informativo: no cambia el análisis ni las decisiones.")
