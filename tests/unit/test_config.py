"""Tests unitarios de app.config.

No requieren base de datos ni ningún servicio externo: son la categoría
"unitarias" de la sección 15.8 del plan.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings


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
    nunca claves de producción por accidente)."""
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
