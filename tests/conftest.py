"""Fixtures compartidas entre tests unitarios y de integración."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_engine


@pytest.fixture(scope="session")
def db_engine():
    """Engine de SQLAlchemy contra la base de datos real de pruebas.

    Requiere que `docker compose up -d` (o equivalente) esté corriendo y que
    `alembic upgrade head` ya se haya aplicado — exactamente el flujo que
    describe el README.
    """
    settings = get_settings()
    return get_engine(settings)


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
