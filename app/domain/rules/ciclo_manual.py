"""Aviso antes de lanzar A MANO un ciclo de análisis cuando ya hay uno reciente.

Por qué existe (observación 2026-10-21): el ciclo manual (`scripts/run_daily_cycle.py`)
no se bloquea por el tope diario de IA — a propósito: lo lanza una persona. Pero
esa misma libertad permitió lanzarlo dos veces seguidas por error (dos ciclos con
18 minutos de diferencia, ~0.36 USD cada uno, sin ningún efecto en propuestas).

Esta regla es PURA: recibe los ciclos ya leídos y devuelve el último "reciente",
o `None`. No toca la base, no pregunta nada y no decide por la persona: solo
permite que el script le avise y le pida confirmación. No es una regla de
riesgo ni de dinero de trading (no afecta cantidades, stops ni ejecución).

"Reciente" reutiliza la misma noción de ciclo que `recovery.py`:

- un ciclo que TERMINÓ sin error (con o sin propuestas) cuenta si terminó dentro
  de la ventana;
- un ciclo que sigue EN CURSO (sin `finished_at`) cuenta si empezó dentro de la
  ventana: lanzar otro encima gastaría IA dos veces para lo mismo;
- un ciclo que FALLÓ (`error_message`) NO cuenta: reintentar tras un fallo es
  legítimo y no debe pedir confirmación.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.rules.recovery import CicloParaRecuperar

VENTANA_CICLO_RECIENTE = timedelta(hours=6)
"""Ventana dentro de la cual un ciclo previo hace pedir confirmación.

TODO — DECISIÓN DEL USUARIO: 6 horas es un valor inicial propuesto (cubre el caso
real: dos corridas con 18 min de diferencia; deja pasar la corrida manual de las
12:15 seguida del ciclo automático de las 19:05, que están a ~7 h). Se cambia
aquí, en un único lugar; no es una variable de entorno a propósito (mismo criterio
que el resto de constantes operativas del proyecto)."""


@dataclass(frozen=True)
class CicloReciente:
    inicio: datetime
    fin: datetime | None
    """`None` = el ciclo sigue en curso."""

    @property
    def en_curso(self) -> bool:
        return self.fin is None

    @property
    def referencia(self) -> datetime:
        return self.inicio if self.fin is None else self.fin


def _exigir_utc(valor: datetime, nombre: str) -> None:
    if valor.tzinfo is None or valor.utcoffset() != timedelta(0):
        raise ValueError(f"`{nombre}` debe ser un datetime con zona horaria UTC explícita (sección 15.3 del plan).")


def buscar_ciclo_reciente(
    ciclos: Sequence[CicloParaRecuperar],
    *,
    now: datetime,
    ventana: timedelta = VENTANA_CICLO_RECIENTE,
) -> CicloReciente | None:
    """El ciclo más reciente que obliga a confirmar, o `None` si no hay ninguno.

    El borde es inclusivo: un ciclo que terminó exactamente hace `ventana` aún
    cuenta como reciente."""
    _exigir_utc(now, "now")
    if ventana <= timedelta(0):
        raise ValueError("`ventana` debe ser positiva.")
    desde = now - ventana
    candidatos: list[CicloReciente] = []
    for ciclo in ciclos:
        if ciclo.error_message:
            continue
        reciente = CicloReciente(inicio=ciclo.started_at, fin=ciclo.finished_at)
        if desde <= reciente.referencia <= now:
            candidatos.append(reciente)
    if not candidatos:
        return None
    return max(candidatos, key=lambda c: c.referencia)


def _hace(delta: timedelta) -> str:
    minutos = int(delta.total_seconds() // 60)
    horas, resto = divmod(minutos, 60)
    if horas == 0:
        return f"{minutos} min"
    return f"{horas} h {resto} min"


def mensaje_ciclo_reciente(ciclo: CicloReciente, *, now: datetime) -> str:
    """Texto del aviso, solo con lo que el ciclo ya contiene (P3)."""
    _exigir_utc(now, "now")
    if ciclo.en_curso:
        return (
            f"ATENCIÓN: hay un ciclo EN CURSO que empezó hace {_hace(now - ciclo.inicio)} "
            f"({ciclo.inicio:%Y-%m-%d %H:%M} UTC). Lanzar otro gastaría IA otra vez para lo mismo."
        )
    return (
        f"ATENCIÓN: ya hay un ciclo completado hace {_hace(now - ciclo.referencia)} "
        f"(terminó {ciclo.referencia:%Y-%m-%d %H:%M} UTC). Otro ciclo gastaría IA otra vez "
        "(~0.2 a 0.5 USD) sobre prácticamente los mismos datos."
    )
