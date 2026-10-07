"""Aviso de modo solo lectura (Paso 11-0): motivo común para deshabilitar acciones."""

from __future__ import annotations

from app.services import views

MOTIVO_SOLO_LECTURA = (
    "Esta interfaz está conectada con un usuario de solo lectura: las acciones que escriben "
    "solo están disponibles en la interfaz local, conectada con el usuario de escritura."
)


def motivo_solo_lectura() -> str | None:
    """`MOTIVO_SOLO_LECTURA` si la conexión no puede escribir; si no, `None`."""
    return MOTIVO_SOLO_LECTURA if views.solo_lectura() else None
