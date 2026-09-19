"""Fixtures compartidas entre tests unitarios y de integración."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import get_engine


def _resolve_test_database_url(settings: Settings) -> str:
    """Devuelve la URL de la base de datos de PRUEBAS, nunca la de la
    aplicación real.

    Bug real encontrado al correr los tests contra datos reales ya
    backfillizados (2026-09-19): `db_engine` apuntaba directamente a
    `settings.database_url` — la misma base que usa la app en marcha. Con
    la base vacía esto era inofensivo, pero en cuanto existieron velas
    reales de producción, los tests que cuentan filas por símbolo/temporalidad
    (`db_session.query(Vela).filter_by(...).count()`) empezaron a contar
    también las reales, y un test con una fecha fija (`test_schema_idempotency.py`)
    chocó con una vela real ya existente en esa misma fecha — la restricción
    única hizo exactamente lo que debía, pero contra la base equivocada.

    La base de pruebas debe ser una base de datos completamente distinta,
    nunca la misma con una tabla vaciada a mitad de la suite: eso arriesgaría
    borrar datos reales por un bug en un test. Si `TEST_DATABASE_URL` está
    definida, se usa tal cual (útil en CI con una base de test dedicada);
    si no, se deriva de `DATABASE_URL` agregando el sufijo `_test` al nombre
    de la base, así no hace falta ninguna variable de entorno nueva para el
    caso común (ver README, sección "Tests", para el comando que crea esa
    base una vez).
    """
    if settings.test_database_url:
        return settings.test_database_url

    url = make_url(settings.database_url)
    if url.database is None:
        raise ValueError(
            "DATABASE_URL no incluye un nombre de base de datos; no se puede "
            "derivar una base de pruebas. Define TEST_DATABASE_URL explícitamente."
        )
    # `str(url)` enmascara la contraseña (`***`) por seguridad en logs/repr —
    # necesitamos la real para poder conectarnos.
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def db_engine():
    """Engine de SQLAlchemy contra la base de datos de PRUEBAS — NUNCA la
    base de datos real de la aplicación (ver `_resolve_test_database_url`).

    Requiere que exista la base de pruebas (por defecto `<nombre>_test`) y
    que `alembic upgrade head` ya se haya aplicado sobre ELLA, no solo sobre
    la base real — ver el README, sección "Tests".
    """
    settings = get_settings()
    test_url = _resolve_test_database_url(settings)
    test_settings = settings.model_copy(update={"database_url": test_url})
    return get_engine(test_settings)


@pytest.fixture()
def db_session(db_engine) -> Iterator[Session]:
    """Sesión de base de datos aislada por test, inmune a los `commit()`
    que haga el propio código bajo prueba.

    Un `session.rollback()` simple al final NO basta: código como
    `candle_ingestion.backfill_all` hace `commit()` después de cada
    símbolo/temporalidad a propósito (para no perder progreso si el
    proceso se corta a mitad de camino — P5 del plan), y esos commits
    quedarían permanentes en la base de datos de pruebas. Eso es
    exactamente lo que produjo un bug real durante el desarrollo: dos
    tests en archivos distintos insertando BTCUSDT en fechas de enero de
    2024 chocaban entre sí porque el primero había dejado sus filas
    comprometidas de verdad.

    El arreglo es el patrón estándar de SQLAlchemy para aislar tests:
    abrir una transacción externa a nivel de conexión que nunca se
    compromete de verdad, y un SAVEPOINT anidado dentro de ella que se
    reabre automáticamente cada vez que el código bajo prueba llama a
    `commit()`. Al terminar el test se revierte la transacción externa
    completa, sin importar cuántos `commit()` haya hecho el código
    probado.
    """
    connection = db_engine.connect()
    outer_transaction = connection.begin()
    session = Session(bind=connection, autoflush=False, expire_on_commit=False)

    nested = connection.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(session_, transaction_):
        nonlocal nested
        if not nested.is_active:
            nested = connection.begin_nested()

    try:
        yield session
    finally:
        session.close()
        outer_transaction.rollback()
        connection.close()
