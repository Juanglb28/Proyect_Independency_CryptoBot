"""Punto de entrada del orquestador.

En este primer entregable, `main.py` solo expone un comando `healthcheck`
que confirma que la infraestructura base funciona: configuración válida,
conexión a la base de datos, y extensión TimescaleDB instalada. No hay
todavía ciclo de análisis, ingesta, ni ejecución (ver sección 16 del plan:
"Primer entregable técnico").

Uso:
    python -m app.main healthcheck
"""

from __future__ import annotations

import sys

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.logging_config import configure_logging
from app.repositories.health import check_database_health

import logging

logger = logging.getLogger(__name__)


def run_healthcheck() -> int:
    """Ejecuta el health check y devuelve un código de salida (0 = OK, 1 = fallo)."""
    settings = get_settings()
    engine = get_engine(settings)
    session_factory = get_sessionmaker(engine)

    with session_scope(session_factory) as session:
        result = check_database_health(session)

    if result.database_reachable and result.timescaledb_installed:
        logger.info("healthcheck OK", extra={"component": "main.healthcheck"})
        print(f"OK: {result.detail}")
        return 0

    logger.error(
        "healthcheck FAILED",
        extra={
            "component": "main.healthcheck",
            "database_reachable": result.database_reachable,
            "timescaledb_installed": result.timescaledb_installed,
        },
    )
    print(f"FALLO: {result.detail}")
    return 1


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    argv = argv if argv is not None else sys.argv[1:]

    if not argv or argv[0] != "healthcheck":
        print("Uso: python -m app.main healthcheck")
        return 2

    return run_healthcheck()


if __name__ == "__main__":
    raise SystemExit(main())
