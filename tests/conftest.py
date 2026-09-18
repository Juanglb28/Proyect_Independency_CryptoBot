"""Fixtures compartidas entre tests unitarios y de integración."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker


@pytest.fixture(scope="session")
def db_session_factory():
    """Fábrica de sesiones contra la base de datos real de pruebas.

    Requiere que `docker compose up -d` (o equivalente) esté corriendo y que
    `alembic upgrade head` ya se haya aplicado — exactamente el flujo que
    describe el README. Los tests de integración de este módulo no crean ni
    destruyen el esquema; solo insertan y limpian sus propias filas.
    """
    settings = get_settings()
    engine = get_engine(settings)
    return get_sessionmaker(engine)


@pytest.fixture()
def db_session(db_session_factory) -> Iterator[Session]:
    """Sesión de base de datos con rollback automático al terminar el test.

    Cada test ve una base de datos limpia de sus propios cambios, sin
    necesidad de un TRUNCATE manual ni de recrear el esquema entre tests.
    """
    session = db_session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
