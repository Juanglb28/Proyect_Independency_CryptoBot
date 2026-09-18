"""Motor y fábrica de sesiones de SQLAlchemy.

Nada fuera de `app/repositories/` debe importar `Session` directamente de
este módulo para hacer consultas de negocio (ver sección 15.2: `repositories`
es la única capa que conoce SQLAlchemy). `main.py` y los tests sí importan
`get_engine`/`get_sessionmaker` para construir la sesión que luego se pasa
a un repositorio.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings


def get_engine(settings: Settings) -> Engine:
    """Crea el engine de SQLAlchemy a partir de la configuración validada."""
    return create_engine(settings.database_url, pool_pre_ping=True)


def get_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Context manager que abre una sesión, hace commit si todo va bien, y
    rollback si algo falla. Uso:

        engine = get_engine(settings)
        factory = get_sessionmaker(engine)
        with session_scope(factory) as session:
            ...
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
