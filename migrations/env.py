"""Entorno de Alembic.

La URL de conexión viene siempre de `app.config.get_settings()` (que a su vez
valida `.env`), nunca de `alembic.ini` ni de una variable leída aquí
directamente. Esto evita tener la cadena de conexión duplicada y
potencialmente desincronizada en dos lugares.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.db.base import Base

# Importa todos los modelos para que Base.metadata los conozca antes del
# autogenerate (ver app/db/models/__init__.py).
import app.db.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Inyecta la URL real de la base de datos, validada por pydantic-settings,
# sobreescribiendo el valor vacío de alembic.ini.
config.set_main_option("sqlalchemy.url", get_settings().database_url)


def run_migrations_offline() -> None:
    """Genera el SQL de las migraciones sin conectarse a la base de datos."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Aplica las migraciones conectándose de verdad a la base de datos."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
