"""Sesión de SOLO LECTURA para la interfaz.

`read_session()` abre una transacción marcada `READ ONLY` en PostgreSQL y la
descarta al salir (nunca hace commit). Es la garantía técnica de que nada de
lo que se ejecute desde la interfaz puede escribir en la base: una escritura
fallaría con un error de la propia base, no por una convención del código.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db.session import get_engine, get_sessionmaker
from app.repositories.health import set_transaction_read_only


@lru_cache(maxsize=1)
def _factory() -> sessionmaker[Session]:
    return get_sessionmaker(get_engine(get_settings()))


def settings() -> Settings:
    """Configuración validada (sin exponer secretos a la interfaz: la
    interfaz solo debe usar los valores no secretos, como el capital)."""
    return get_settings()


@contextmanager
def read_session(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    session = (factory or _factory())()
    try:
        set_transaction_read_only(session)
        yield session
    finally:
        session.rollback()
        session.close()
