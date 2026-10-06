"""Sesión de ESCRITURA compartida por los servicios que guardan algo
(`commands`, `interview`). Hace commit al salir bien y rollback si falla. Las
lecturas NO usan esto: usan `app.services.session.read_session` (READ ONLY)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_engine, get_sessionmaker
from app.services.session import settings


@dataclass(frozen=True)
class ResultadoAccion:
    """Resultado de una acción manual que escribe: `ok`, un código estable y un
    mensaje apto para mostrarse tal cual al usuario."""

    ok: bool
    codigo: str
    mensaje: str


@lru_cache(maxsize=1)
def escritura_factory() -> sessionmaker[Session]:
    return get_sessionmaker(get_engine(settings()))
