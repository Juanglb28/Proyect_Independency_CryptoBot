"""Crea (o actualiza) el rol de SOLO LECTURA de la interfaz (Paso 11-0).

Se ejecuta con la conexión de ADMINISTRACIÓN (`DATABASE_URL`, el usuario dueño
de la base), y la contraseña del rol nuevo se toma de la variable de entorno
`UI_DB_PASSWORD` (nunca como argumento ni en el repositorio, y nunca se
imprime). Es idempotente: volver a ejecutarlo actualiza contraseña y permisos.
Hay que repetirlo tras restaurar un volcado o añadir tablas con migraciones.

Uso (PowerShell, ver README):
    docker compose run --rm -e DATABASE_URL -e UI_DB_PASSWORD app python -m scripts.create_ui_role
"""

from __future__ import annotations

import os
import sys

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.logging_config import configure_logging
from app.repositories.db_roles import LONGITUD_MINIMA_CONTRASENA, ensure_readonly_role

ROL_UI = "crypto_ui"


def main() -> int:
    configure_logging()
    contrasena = os.environ.get("UI_DB_PASSWORD", "")
    if len(contrasena) < LONGITUD_MINIMA_CONTRASENA:
        print(f"Falta UI_DB_PASSWORD (mínimo {LONGITUD_MINIMA_CONTRASENA} caracteres). No se hizo ningún cambio.")
        return 2
    engine = get_engine(get_settings())
    with get_sessionmaker(engine)() as session:
        ensure_readonly_role(session, role=ROL_UI, password=contrasena)
        session.commit()
    print(f"Rol de solo lectura '{ROL_UI}' listo. Verifica con: python -m scripts.db_summary (usando ese usuario).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
