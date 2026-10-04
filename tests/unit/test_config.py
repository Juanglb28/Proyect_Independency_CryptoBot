"""Tests unitarios de app.config.

No requieren base de datos ni ningún servicio externo: son la categoría
"unitarias" de la sección 15.8 del plan.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings


_VARIABLES_DE_SETTINGS = (
    "DATABASE_URL",
    "TEST_DATABASE_URL",
    "PAPER_CAPITAL_USD",
    "MAX_OPEN_POSITIONS",
    "BINANCE_TESTNET",
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_SALDO_DECLARADO_USD",
    "ANTHROPIC_SALDO_DECLARADO_EN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)


@pytest.fixture(autouse=True)
def _entorno_limpio(monkeypatch):
    """Borra del entorno del proceso toda variable que lee `Settings`, para
    que cada test parta de cero sin importar dónde corra (venv nativo, o
    `docker compose run`, que inyecta el `.env` completo — incluidas claves
    vacías como `BINANCE_API_KEY=`, que ganan sobre el default `None`)."""
    for nombre in _VARIABLES_DE_SETTINGS:
        monkeypatch.delenv(nombre, raising=False)


def test_settings_requires_database_url(monkeypatch):
    """Sin DATABASE_URL, la aplicación debe fallar explícitamente al arrancar
    (sección 15.4 del plan), no más tarde con un error críptico.

    `monkeypatch.delenv` es necesario porque pydantic-settings lee el
    entorno del proceso independientemente de `_env_file=None` (ese
    parámetro solo desactiva la lectura de un archivo `.env`, no de las
    variables de entorno ya exportadas en el shell)."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_defaults_match_plan_values():
    """Los valores por defecto de capital y posiciones deben coincidir
    exactamente con los fijados en la sección 9 del plan (1.000 USD, 4
    posiciones), y binance_testnet debe ser True por defecto (sección 10:
    nunca claves de producción por accidente).

    Bug real (2026-10-04): este test pasaba en venv nativo pero fallaba
    dentro de `docker compose run --rm app pytest` con `'' is None`.
    `docker-compose.yml` inyecta el `.env` completo como variables de
    entorno del contenedor, y `.env.example` deja claves como
    `BINANCE_API_KEY=` vacías: una variable de entorno definida pero vacía
    gana sobre el default `None` de `Settings`, aunque se pase
    `_env_file=None` (ese parámetro solo desactiva el archivo, no el
    entorno del proceso — mismo motivo que `test_settings_requires_database_url`).
    Ninguno de estos tests puede depender del entorno de quien los corra:
    ver la fixture autouse `_entorno_limpio` de este archivo."""
    settings = Settings(_env_file=None, database_url="postgresql+psycopg://x:x@localhost/x")  # type: ignore[call-arg]

    assert settings.paper_capital_usd == Decimal("1000")
    assert settings.max_open_positions == 4
    assert settings.binance_testnet is True
    assert settings.binance_api_key is None
    assert settings.anthropic_api_key is None
    assert settings.anthropic_saldo_declarado_usd is None
    assert settings.anthropic_saldo_declarado_en is None


def test_settings_reads_from_env_vars(monkeypatch):
    """La configuración debe poder venir de variables de entorno reales, no
    solo de valores pasados explícitamente (así es como corre en producción)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://a:b@host/db")
    monkeypatch.setenv("PAPER_CAPITAL_USD", "500")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "2")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.database_url == "postgresql+psycopg://a:b@host/db"
    assert settings.paper_capital_usd == Decimal("500")
    assert settings.max_open_positions == 2


def test_settings_reads_saldo_declarado_de_anthropic(monkeypatch):
    # Bug real (2026-10-02): el usuario detectó que el costo reportado por
    # este sistema no cuadraba con el saldo real de su cuenta de Anthropic.
    # Ver app/ai/models.py::calculate_cost_usd (la causa real) y este par de
    # campos (para poder ESTIMAR cuánto queda, declarado por el usuario,
    # nunca inventado — P3 del plan).
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://a:b@host/db")
    monkeypatch.setenv("ANTHROPIC_SALDO_DECLARADO_USD", "2.26")
    monkeypatch.setenv("ANTHROPIC_SALDO_DECLARADO_EN", "2026-10-02T00:00:00+00:00")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.anthropic_saldo_declarado_usd == Decimal("2.26")
    assert settings.anthropic_saldo_declarado_en == datetime(2026, 10, 2, tzinfo=timezone.utc)
