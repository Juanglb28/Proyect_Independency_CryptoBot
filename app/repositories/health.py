"""Health check de base de datos.

Único propósito: confirmar que la aplicación puede conectarse a PostgreSQL y
que la extensión TimescaleDB está instalada. No contiene lógica de negocio
(sección 15.2: `repositories` es solo persistencia).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class HealthCheckResult:
    database_reachable: bool
    timescaledb_installed: bool
    detail: str


def check_database_health(session: Session) -> HealthCheckResult:
    """Ejecuta un `SELECT 1` y verifica la extensión `timescaledb`.

    No lanza excepciones hacia arriba: cualquier fallo de conexión se
    convierte en un `HealthCheckResult` con `database_reachable=False`, para
    que quien llame (CLI, futuro endpoint de salud) pueda decidir qué hacer
    sin tener que envolver la llamada en try/except.
    """
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — health check: cualquier fallo es "no saludable"
        return HealthCheckResult(
            database_reachable=False,
            timescaledb_installed=False,
            detail=f"No se pudo conectar a la base de datos: {exc}",
        )

    try:
        row = session.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'timescaledb'")
        ).first()
    except Exception as exc:  # noqa: BLE001
        return HealthCheckResult(
            database_reachable=True,
            timescaledb_installed=False,
            detail=f"Conexión OK, pero no se pudo verificar la extensión timescaledb: {exc}",
        )

    if row is None:
        return HealthCheckResult(
            database_reachable=True,
            timescaledb_installed=False,
            detail="Conexión OK, pero la extensión timescaledb no está instalada.",
        )

    return HealthCheckResult(
        database_reachable=True,
        timescaledb_installed=True,
        detail="Conexión OK. Extensión timescaledb instalada.",
    )
