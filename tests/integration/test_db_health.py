"""Test de integración: health check contra una base de datos real.

Requiere `docker compose up -d` y `alembic upgrade head` ya ejecutados
(ver README). Esta es la categoría "integración" de la sección 15.8 del plan.
"""

from app.repositories.health import check_database_health


def test_database_is_reachable(db_session):
    result = check_database_health(db_session)
    assert result.database_reachable is True


def test_timescaledb_extension_is_installed(db_session):
    """La extensión TimescaleDB debe estar instalada porque `velas` depende
    de ella para ser una hipertabla (sección 6 y 15.1 del plan). Si esta
    prueba falla, casi siempre significa que se está corriendo contra un
    PostgreSQL plano en vez de la imagen `timescale/timescaledb` del
    `docker-compose.yml` de este proyecto."""
    result = check_database_health(db_session)
    assert result.timescaledb_installed is True, result.detail
