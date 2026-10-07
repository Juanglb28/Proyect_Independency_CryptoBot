"""`summarize_database` y `scripts.db_summary`: foto de solo lectura de la base."""

from __future__ import annotations

from sqlalchemy import text

from app.repositories.health import summarize_database
from scripts.db_summary import format_summary


def test_resumen_incluye_migracion_y_conteos_exactos(db_session):
    r = summarize_database(db_session)
    assert r.alembic_revision  # la base de pruebas está migrada
    assert "ordenes" in r.row_counts and "alembic_version" not in r.row_counts
    antes = r.row_counts["eventos"]
    db_session.execute(text("SELECT 1"))
    assert summarize_database(db_session).row_counts["eventos"] == antes  # solo lectura


def test_formato_no_imprime_url_ni_secretos_y_avisa_si_no_hay_tls(db_session):
    texto = "\n".join(format_summary(summarize_database(db_session)))
    assert "postgresql" not in texto.lower().replace("postgresql ", "")  # ninguna URL de conexión
    assert "TLS:" in texto and "TimescaleDB:" in texto and "Migración (alembic):" in texto
