"""Reglas deterministas de RECUPERACIÓN al arrancar (Paso 11c; principio P5).

Funciones puras: reciben filas simples ya leídas y devuelven un veredicto o un
texto. No leen la base de datos, no llaman a Binance, no envían nada y no
conocen ningún LLM (P1). Avisar NUNCA cambia una orden, una posición ni una
regla de riesgo.

Decisión del usuario (2026-10-10): si el scheduler arranca después de la hora
programada del ciclo diario y hoy no hay un ciclo completo, el sistema AVISA y
NO relanza el ciclo por su cuenta. Relanzarlo gastaría créditos de IA sin que
nadie lo pida (y se repetiría con cada reinicio); el usuario decide si lo
lanza a mano.

"Ciclo completo de hoy" = un ciclo iniciado hoy (día UTC) que terminó
(`finished_at`) sin error (`error_message` vacío). Un ciclo que terminó sin
candidatos cuenta como completo: el pre-filtro hizo su trabajo. Un ciclo
disparado a mitad de día por candidatos nuevos (E3) también cuenta: es el mismo
análisis completo.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

HORA_CICLO_DIARIO_UTC = time(0, 5)
"""Hora (UTC) a la que el scheduler lanza el ciclo diario: justo después del
cierre de la vela diaria. Es la ÚNICA definición: el scheduler la usa para su
disparador y la recuperación para saber si ya debía haber corrido."""

MENSAJE_CICLO_INTERRUMPIDO = (
    "Interrumpido: el proceso se reinició antes de terminar el ciclo (detectado al arrancar el scheduler)."
)


@dataclass(frozen=True)
class CicloParaRecuperar:
    id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None
    error_message: str | None


def _exigir_utc(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("`now` debe ser un datetime con zona horaria UTC explícita (sección 15.3 del plan).")


def inicio_del_dia_utc(now: datetime) -> datetime:
    _exigir_utc(now)
    return datetime.combine(now.astimezone(timezone.utc).date(), time.min, tzinfo=timezone.utc)


def hay_ciclo_diario_perdido(ciclos: Sequence[CicloParaRecuperar], *, now: datetime) -> bool:
    """`True` si ya pasó la hora programada del ciclo diario de HOY (UTC) y no
    existe un ciclo completo iniciado hoy (ver el docstring del módulo).

    Los ciclos sin terminar (`finished_at` vacío) nunca cuentan como completos:
    quien llama debe haberlos marcado como interrumpidos antes, o ignorarlos.
    """
    inicio = inicio_del_dia_utc(now)
    if now < datetime.combine(inicio.date(), HORA_CICLO_DIARIO_UTC, tzinfo=timezone.utc):
        return False
    completo = any(
        c.started_at >= inicio and c.finished_at is not None and not c.error_message for c in ciclos
    )
    return not completo


def mensaje_ciclo_diario_perdido(dia: date, *, interrumpidos: int) -> str:
    detalle = (
        f" Había {interrumpidos} ciclo(s) a medias que se marcaron como interrumpidos."
        if interrumpidos
        else ""
    )
    return (
        f"CICLO DIARIO PERDIDO: el scheduler arrancó después de las {HORA_CICLO_DIARIO_UTC:%H:%M} UTC del "
        f"{dia.isoformat()} y no hay un ciclo de análisis completo de hoy.{detalle} NO se relanzó solo "
        f"(gastaría IA sin que lo pidas): lánzalo a mano si lo quieres."
    )
