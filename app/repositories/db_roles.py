"""Roles de base de datos (Paso 11-0): el rol de SOLO LECTURA de la interfaz.

La interfaz desplegada fuera del equipo local no necesita escribir nunca. En
vez de confiar solo en el código (`SET TRANSACTION READ ONLY`), se le da un rol
de PostgreSQL que NO tiene privilegios de escritura: aunque alguien cambiara el
código o inyectara SQL, la base lo rechazaría (defensa en profundidad, P4).

Solo persistencia de roles/privilegios: sin lógica de negocio. Los nombres y la
contraseña NUNCA se concatenan en el SQL: se arman con `format(%I, %L)` en el
servidor, que los escapa como identificador y literal.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.orm import Session

_NOMBRE_VALIDO = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
LONGITUD_MINIMA_CONTRASENA = 16

# Tabla usada como sonda del privilegio de escritura: si el rol puede insertar
# en `ordenes` (la tabla más sensible), no es un rol de solo lectura.
_TABLA_SONDA = "ordenes"


def _ejecutar_formato(session: Session, plantilla: str, **params: str) -> None:
    """Construye la sentencia con `format()` EN EL SERVIDOR y la ejecuta."""
    for nombre in params:  # tipo explícito: el servidor no puede inferirlo dentro de format()
        plantilla = plantilla.replace(f":{nombre}", f"CAST(:{nombre} AS text)")
    sentencia = session.execute(text(f"SELECT format({plantilla})"), params).scalar_one()
    session.execute(text(sentencia))


def ensure_readonly_role(session: Session, *, role: str, password: str) -> None:
    """Crea (o actualiza) un rol que solo puede leer. Es idempotente: se puede
    volver a ejecutar, y DEBE ejecutarse otra vez tras restaurar un volcado o
    migrar (para cubrir tablas nuevas). No hace commit: lo hace quien llama.

    - LOGIN, sin superusuario/crear bases/crear roles.
    - `default_transaction_read_only = on` (cinturón; el tirante son los privilegios).
    - Solo `SELECT` sobre las tablas del esquema `public` (existentes y futuras).
    """
    if not _NOMBRE_VALIDO.match(role):
        raise ValueError("Nombre de rol no válido (minúsculas, dígitos y guion bajo; 3 a 41 caracteres).")
    if len(password) < LONGITUD_MINIMA_CONTRASENA:
        raise ValueError(f"La contraseña del rol debe tener al menos {LONGITUD_MINIMA_CONTRASENA} caracteres.")

    existe = session.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first()
    opciones = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION LOGIN"
    if existe:
        _ejecutar_formato(session, f"'ALTER ROLE %I {opciones} PASSWORD %L', :r, :p", r=role, p=password)
    else:
        _ejecutar_formato(session, f"'CREATE ROLE %I {opciones} PASSWORD %L', :r, :p", r=role, p=password)

    _ejecutar_formato(session, "'ALTER ROLE %I SET default_transaction_read_only = on', :r", r=role)
    _ejecutar_formato(session, "'GRANT CONNECT ON DATABASE %I TO %I', current_database(), :r", r=role)
    _ejecutar_formato(session, "'GRANT USAGE ON SCHEMA public TO %I', :r", r=role)
    # Parte de cero (quita lo que pudiera haber) y concede solo lectura.
    _ejecutar_formato(session, "'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', :r", r=role)
    _ejecutar_formato(session, "'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', :r", r=role)
    _ejecutar_formato(session, "'GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I', :r", r=role)
    # Tablas que cree después el usuario que ejecuta este script (migraciones).
    _ejecutar_formato(session, "'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO %I', :r", r=role)


def has_write_access(session: Session) -> bool:
    """¿El usuario actual de la conexión puede escribir? Se mira el privilegio
    REAL (`has_table_privilege`), no una variable de configuración que el propio
    usuario podría cambiar."""
    return bool(
        session.execute(
            text("SELECT has_table_privilege(current_user, :t, 'INSERT')"), {"t": _TABLA_SONDA}
        ).scalar_one()
    )
