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


def set_transaction_read_only(session: Session) -> None:
    """Marca la transacción ACTUAL como de solo lectura: cualquier escritura
    (INSERT/UPDATE/DELETE/DDL) falla en la base. Es la garantía de que los
    servicios de lectura de la interfaz (Paso 10a) no pueden modificar nada,
    aunque alguien se equivoque en el código. Debe llamarse antes de la
    primera consulta de la transacción."""
    session.execute(text("SET TRANSACTION READ ONLY"))


@dataclass(frozen=True)
class DatabaseSummary:
    """Foto de solo lectura de una base: sirve para comprobar un despliegue
    (¿TLS activo? ¿extensión? ¿última migración?) y para comparar conteos por
    tabla antes y después de una restauración (Paso 11-0)."""

    server_version: str
    ssl_in_use: bool
    ssl_version: str | None
    timescaledb_version: str | None
    alembic_revision: str | None
    row_counts: dict[str, int]


def summarize_database(session: Session) -> DatabaseSummary:
    """Solo lectura (SELECT). Los conteos son exactos (`count(*)`), no
    estimaciones, porque se usan para verificar una restauración.

    Los nombres de tabla salen del catálogo (`pg_tables`) y se entregan a
    SQLAlchemy como identificadores (`quoted_name`): nunca se concatenan en el
    SQL. `alembic_version` se excluye de los conteos (se informa aparte)."""
    from sqlalchemy import func, select
    from sqlalchemy.sql import column, table

    version = session.execute(text("SHOW server_version")).scalar_one()
    ssl_row = session.execute(
        text("SELECT ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
    ).first()
    timescale = session.execute(
        text("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
    ).scalar_one_or_none()
    try:
        with session.begin_nested():
            revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - base sin migrar: no hay tabla
        revision = None

    nombres = session.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version' ORDER BY tablename")
    ).scalars().all()
    counts = {
        nombre: session.execute(select(func.count()).select_from(table(nombre, column("x")))).scalar_one()
        for nombre in nombres
    }
    return DatabaseSummary(
        server_version=str(version),
        ssl_in_use=bool(ssl_row[0]) if ssl_row else False,
        ssl_version=ssl_row[1] if ssl_row and ssl_row[0] else None,
        timescaledb_version=timescale,
        alembic_revision=revision,
        row_counts=counts,
    )
