"""Interruptor de emergencia: el archivo `PARAR` (sección 9 del plan).

> "Un archivo `PARAR` en disco detiene toda operación nueva sin necesidad de
> matar el proceso."

Si existe un archivo llamado `PARAR` en la raíz del proyecto, el motor no
abre ni aprueba ninguna entrada nueva. Basta con crearlo (vacío) para
frenar y borrarlo para soltar el freno; no hace falta reiniciar nada porque
se consulta en cada operación.

Decisión derivada del plan (a corregir si no es lo que quieres): `PARAR`
bloquea solo las operaciones NUEVAS (abrir y aprobar entradas), igual que la
pausa por drawdown. El monitor sigue vigilando las posiciones abiertas y
ejecutando sus stops: el plan dice que "si Juan no responde, el stop sigue
activo y limita la pérdida de todas formas", y apagar los stops con una
parada de emergencia dejaría las posiciones sin protección justo cuando algo
anda mal.

La raíz del proyecto es la carpeta que contiene `app/`, así que el archivo
queda en el mismo lugar con o sin Docker (el repo se monta en `/app`).
Solo se mira SI EXISTE: nunca se lee su contenido.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

PARAR_FILENAME = "PARAR"
PARAR_PATH = Path(__file__).resolve().parents[2] / PARAR_FILENAME


class EmergencyStop(Protocol):
    def is_active(self) -> bool: ...


class FileEmergencyStop:
    """`EmergencyStop` real: activo si existe el archivo `PARAR`. La ruta se
    resuelve al consultar (no al importar) para poder cambiarla en pruebas."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    def is_active(self) -> bool:
        return (self._path if self._path is not None else PARAR_PATH).exists()
