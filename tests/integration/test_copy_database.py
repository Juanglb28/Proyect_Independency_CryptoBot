"""`scripts.copy_database`: copia de datos entre dos bases del mismo esquema.

Se crean dos bases de usar y tirar con el esquema de la base de pruebas
(`pg_dump --schema-only`), se llena la de origen con datos reales del sistema
(ciclo → propuesta → orden, eventos con ids de identidad) y se copia.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import get_settings
from scripts.copy_database import CopiaAbortada, conectar, copiar, main
from tests.conftest import _resolve_test_database_url
from tests.integration.test_commands import _pausar_sin_posiciones, _pendiente

pytestmark = pytest.mark.skipif(
    not (shutil.which("pg_dump") and shutil.which("psql")), reason="requiere pg_dump y psql"
)


def _env(url):
    return {**os.environ, "PGPASSWORD": url.password or ""}


def _args(url):
    return ["-h", url.host or "localhost", "-p", str(url.port or 5432), "-U", url.username]


@pytest.fixture
def bases():
    base = make_url(_resolve_test_database_url(get_settings()))
    sufijo = uuid.uuid4().hex[:8]
    nombres = {"o": f"copytest_o_{sufijo}", "d": f"copytest_d_{sufijo}"}
    admin = psycopg.connect(base.set(drivername="postgresql").render_as_string(hide_password=False), autocommit=True)
    try:
        esquema = subprocess.run(
            ["pg_dump", "--schema-only", "--no-owner", "--no-privileges", *_args(base), base.database],
            env=_env(base), capture_output=True, check=True, text=True,
        ).stdout
        urls = {}
        for k, nombre in nombres.items():
            admin.execute(f'CREATE DATABASE "{nombre}"')
            url = base.set(database=nombre)
            subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", *_args(base), nombre], env=_env(base),
                           input=esquema, text=True, check=True, capture_output=True)
            urls[k] = url.render_as_string(hide_password=False)
            _alembic(urls[k], "revision_de_prueba")
        yield urls
    finally:
        for nombre in nombres.values():
            admin.execute(f'DROP DATABASE IF EXISTS "{nombre}" WITH (FORCE)')
        admin.close()


def _llenar(url_origen):
    engine = create_engine(url_origen)
    with Session(engine) as s:
        _pendiente(s)
        _pausar_sin_posiciones(s)
        s.commit()
    engine.dispose()


def _alembic(url, version):
    with psycopg.connect(make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)) as c:
        c.execute("DELETE FROM alembic_version")
        c.execute("INSERT INTO alembic_version VALUES (%s)", (version,))


def _conteos(url):
    with conectar(url) as c:
        tablas = [r[0] for r in c.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'")]
        return {t: c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in sorted(tablas)}


def test_copia_todo_y_reajusta_las_secuencias(bases):
    _llenar(bases["o"])
    esperado = _conteos(bases["o"])
    assert esperado["ordenes"] == 1 and esperado["eventos_sistema"] == 1
    with conectar(bases["o"]) as o, conectar(bases["d"]) as d:
        r = copiar(o, d, ejecutar=True)
    assert r.ejecutada and r.filas == esperado and _conteos(bases["d"]) == esperado
    with conectar(bases["d"]) as d:  # el siguiente id de identidad no choca con los copiados
        d.execute("INSERT INTO eventos_sistema (event_type, occurred_at, equity_usd, peak_equity_usd) "
            "SELECT event_type, occurred_at, equity_usd, peak_equity_usd FROM eventos_sistema LIMIT 1")
        ids = [x[0] for x in d.execute("SELECT id FROM eventos_sistema ORDER BY id")]
        assert len(ids) == 2 and ids[0] != ids[1]


def test_sin_ejecutar_no_escribe_nada(bases):
    _llenar(bases["o"])
    with conectar(bases["o"]) as o, conectar(bases["d"]) as d:
        r = copiar(o, d, ejecutar=False)
    assert not r.ejecutada and r.filas["ordenes"] == 1
    assert sum(_conteos(bases["d"]).values()) == 0


def test_no_copia_si_el_destino_no_esta_vacio(bases):
    _llenar(bases["o"])
    _llenar(bases["d"])
    antes = _conteos(bases["d"])
    with conectar(bases["o"]) as o, conectar(bases["d"]) as d, pytest.raises(CopiaAbortada, match="NO está vacío"):
        copiar(o, d, ejecutar=True)
    assert _conteos(bases["d"]) == antes


def test_no_copia_si_las_migraciones_difieren(bases):
    _llenar(bases["o"])
    _alembic(bases["d"], "otra_revision")
    with conectar(bases["o"]) as o, conectar(bases["d"]) as d, pytest.raises(CopiaAbortada, match="misma migración"):
        copiar(o, d, ejecutar=True)
    assert sum(_conteos(bases["d"]).values()) == 0


def test_un_fallo_a_mitad_de_la_copia_no_deja_nada(bases):
    _llenar(bases["o"])
    with conectar(bases["d"]) as d:  # el destino rechaza ordenes: la copia falla tras copiar tablas previas
        d.execute("ALTER TABLE ordenes ADD CONSTRAINT nunca CHECK (false) NOT VALID")
    with conectar(bases["o"]) as o, conectar(bases["d"]) as d, pytest.raises(psycopg.Error):
        copiar(o, d, ejecutar=True)
    assert sum(_conteos(bases["d"]).values()) == 0


def test_main_exige_las_variables_y_no_imprime_las_urls(bases, monkeypatch, capsys):
    monkeypatch.delenv("SOURCE_DATABASE_URL", raising=False)
    monkeypatch.delenv("DEST_DATABASE_URL", raising=False)
    assert main([]) == 2
    _llenar(bases["o"])
    monkeypatch.setenv("SOURCE_DATABASE_URL", bases["o"])
    monkeypatch.setenv("DEST_DATABASE_URL", bases["d"])
    assert main(["--ejecutar"]) == 0
    salida = capsys.readouterr().out
    assert "Copiado y verificado" in salida
    assert make_url(bases["o"]).password not in salida and "postgresql" not in salida
