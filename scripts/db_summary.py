"""Resumen de solo lectura de la base a la que apunta `DATABASE_URL`.

Para qué sirve (Paso 11-0, despliegue en Railway):
- confirmar que la conexión usa TLS y que existe la extensión TimescaleDB;
- ver en qué migración está la base;
- listar el número exacto de filas por tabla, para compararlo ANTES y DESPUÉS
  de restaurar un volcado (`pg_dump`/restore) y comprobar que no se perdió nada.

Nunca imprime la URL de conexión (lleva contraseña).

Uso:
    python -m scripts.db_summary
    docker compose run --rm -e DATABASE_URL app python -m scripts.db_summary
"""

from __future__ import annotations

import sys

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.logging_config import configure_logging
from app.repositories.health import set_transaction_read_only, summarize_database


def format_summary(resumen) -> list[str]:
    lineas = [
        f"PostgreSQL {resumen.server_version}",
        "TLS: " + (f"sí ({resumen.ssl_version})" if resumen.ssl_in_use else "NO (conexión sin cifrar)"),
        "TimescaleDB: " + (resumen.timescaledb_version or "no instalada"),
        "Migración (alembic): " + (resumen.alembic_revision or "sin migrar"),
        f"Tablas: {len(resumen.row_counts)}",
    ]
    ancho = max((len(n) for n in resumen.row_counts), default=0)
    lineas += [f"  {nombre:<{ancho}}  {filas}" for nombre, filas in resumen.row_counts.items()]
    return lineas


def main() -> int:
    configure_logging()
    engine = get_engine(get_settings())
    with get_sessionmaker(engine)() as session:
        set_transaction_read_only(session)
        resumen = summarize_database(session)
        session.rollback()
    print("\n".join(format_summary(resumen)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
