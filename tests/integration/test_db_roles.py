"""Rol de solo lectura de la interfaz (Paso 11-0): lo que la BASE le permite, no el código.

Los roles de PostgreSQL son de todo el servidor, pero aquí la creación ocurre
dentro de la transacción de prueba, que se descarta al terminar.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.repositories.db_roles import ensure_readonly_role, has_write_access

pytestmark = pytest.mark.usefixtures("_puede_crear_roles")

ROL = "crypto_ui_prueba"
CLAVE = "clave-de-prueba-larga-123"


@pytest.fixture
def _puede_crear_roles(db_session):
    if not db_session.execute(text("SELECT rolcreaterole OR rolsuper FROM pg_roles WHERE rolname = current_user")).scalar_one():
        pytest.skip("el usuario de pruebas no puede crear roles (en Docker es superusuario)")


def _como_rol(session):
    session.execute(text(f"SET LOCAL ROLE {ROL}"))


def test_el_rol_puede_leer_pero_no_escribir(db_session):
    ensure_readonly_role(db_session, role=ROL, password=CLAVE)
    assert has_write_access(db_session) is True  # el usuario de pruebas es el dueño
    _como_rol(db_session)
    assert has_write_access(db_session) is False
    assert db_session.execute(text("SELECT count(*) FROM ordenes")).scalar_one() >= 0
    assert db_session.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 1


@pytest.mark.parametrize("sentencia", [
    "INSERT INTO perfil_usuario DEFAULT VALUES",
    "INSERT INTO ordenes DEFAULT VALUES",
    "DELETE FROM ordenes",
    "UPDATE posiciones SET status = status",
    "CREATE TABLE intruso (x int)",
    "TRUNCATE ordenes",
])
def test_el_rol_no_puede_escribir_ni_siquiera_desactivando_solo_lectura(db_session, sentencia):
    ensure_readonly_role(db_session, role=ROL, password=CLAVE)
    _como_rol(db_session)
    db_session.execute(text("SET LOCAL transaction_read_only = off"))  # un usuario puede intentarlo
    with pytest.raises(DBAPIError, match="permission denied|must be owner|permiso denegado"):
        with db_session.begin_nested():
            db_session.execute(text(sentencia))


def test_es_idempotente_y_el_rol_no_es_superusuario(db_session):
    ensure_readonly_role(db_session, role=ROL, password=CLAVE)
    ensure_readonly_role(db_session, role=ROL, password=CLAVE + "-nueva")
    fila = db_session.execute(
        text("SELECT rolsuper, rolcreatedb, rolcreaterole, rolcanlogin FROM pg_roles WHERE rolname = :r"), {"r": ROL}
    ).one()
    assert tuple(fila) == (False, False, False, True)
    assert db_session.execute(text("SELECT count(*) FROM pg_roles WHERE rolname = :r"), {"r": ROL}).scalar_one() == 1


def test_rechaza_nombres_y_contrasenas_inseguros(db_session):
    for rol in ("Crypto UI", "x; DROP TABLE ordenes", "a", "1abc", 'a"b'):
        with pytest.raises(ValueError):
            ensure_readonly_role(db_session, role=rol, password=CLAVE)
    with pytest.raises(ValueError):
        ensure_readonly_role(db_session, role=ROL, password="corta")


def test_una_contrasena_con_comillas_no_rompe_ni_inyecta(db_session):
    ensure_readonly_role(db_session, role=ROL, password="it's;\"raro\"--clave-larga-123")
    assert db_session.execute(text("SELECT count(*) FROM pg_roles WHERE rolname = :r"), {"r": ROL}).scalar_one() == 1
