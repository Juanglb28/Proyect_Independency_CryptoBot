"""Modo de acceso de la interfaz (Paso 11-0): ¿la conexión puede escribir?

Si la interfaz se conecta con el rol de solo lectura (despliegue remoto), las
acciones que escriben (aprobar, rechazar, reanudar, guardar el perfil) se
muestran deshabilitadas con una explicación, en vez de fallar al confirmar.
Solo lectura: no decide nada, solo informa.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.repositories.db_roles import has_write_access


def es_solo_lectura(session: Session) -> bool:
    return not has_write_access(session)
