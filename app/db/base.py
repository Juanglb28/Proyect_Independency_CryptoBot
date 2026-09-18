"""Base declarativa de SQLAlchemy.

Todos los modelos ORM (`app/db/models/*.py`) heredan de `Base`. Alembic usa
`Base.metadata` en `migrations/env.py` para el autogenerate de migraciones.

Se fija una convención de nombres para índices y constraints: sin esto,
SQLAlchemy les da nombres autogenerados que cambian entre corridas y hacen
que Alembic no pueda detectar renombrados de forma confiable.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Clase base declarativa para todos los modelos ORM del proyecto."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
