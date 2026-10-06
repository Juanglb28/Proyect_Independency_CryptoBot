"""Aplica `alembic upgrade head` a la base real Y a la base de pruebas.

Por qué existe: los tests de integración corren contra una base separada
(`<nombre>_test`, ver `tests/conftest.py`), y cada migración nueva hay que
aplicarla a las DOS. Aplicarla solo a la real hace fallar los tests con
`relation "..." does not exist` (pasó con `perfil_usuario`, Paso 10b).

No hace nada distinto de `alembic upgrade head`: solo lo repite con la
`DATABASE_URL` de cada base. No crea la base de pruebas (si no existe,
alembic falla con su propio error — ver README, sección "Tests") y nunca
imprime las URLs (llevan contraseña).

Uso:
    python -m scripts.migrate_all
    docker compose run --rm app python -m scripts.migrate_all
"""

from __future__ import annotations

import os
import subprocess
import sys

from sqlalchemy.engine import make_url

from app.config import Settings, get_settings


def bases_a_migrar(settings: Settings) -> list[tuple[str, str]]:
    """`[(etiqueta, url)]`: primero la base real, luego la de pruebas.

    Misma regla que `tests/conftest.py::_resolve_test_database_url`:
    `TEST_DATABASE_URL` si está definida; si no, `<base real>_test`. Se
    repite aquí (en vez de importar desde `tests/`) para que un script de
    operación no dependa del código de pruebas; `tests/unit/test_migrate_all.py`
    verifica que ambas reglas den el mismo resultado.
    """
    url_real = settings.database_url
    if settings.test_database_url:
        url_prueba = settings.test_database_url
    else:
        url = make_url(url_real)
        if url.database is None:
            raise ValueError(
                "DATABASE_URL no incluye un nombre de base de datos; no se puede "
                "derivar la base de pruebas. Define TEST_DATABASE_URL explícitamente."
            )
        url_prueba = url.set(database=f"{url.database}_test").render_as_string(hide_password=False)
    return [("real", url_real), ("pruebas", url_prueba)]


def main() -> int:
    for etiqueta, url in bases_a_migrar(get_settings()):
        print(f"== alembic upgrade head — base {etiqueta} ==", flush=True)
        # La variable de entorno tiene prioridad sobre .env en pydantic-settings,
        # y `migrations/env.py` toma la URL de `get_settings()`.
        resultado = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env={**os.environ, "DATABASE_URL": url},
            check=False,
        )
        if resultado.returncode != 0:
            print(f"FALLÓ la migración de la base {etiqueta}; no se continuó.", file=sys.stderr)
            return resultado.returncode
    print("Listo: ambas bases en la última migración.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
