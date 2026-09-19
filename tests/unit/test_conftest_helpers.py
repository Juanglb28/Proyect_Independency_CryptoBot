"""Test de la lógica que decide qué base de datos usan los tests de
integración — no es un detalle menor: un fallo aquí haría que los tests
corrieran contra la base de datos real (ver tests/conftest.py para el bug
real que motivó esto)."""

from app.config import Settings
from tests.conftest import _resolve_test_database_url


def _settings(**overrides) -> Settings:
    defaults = dict(database_url="postgresql+psycopg://crypto:crypto@localhost:5432/crypto_trading")
    defaults.update(overrides)
    return Settings(**defaults)


def test_derives_test_database_by_appending_suffix_when_not_overridden():
    settings = _settings()
    assert (
        _resolve_test_database_url(settings)
        == "postgresql+psycopg://crypto:crypto@localhost:5432/crypto_trading_test"
    )


def test_never_returns_the_real_database_url():
    """La base de pruebas NUNCA debe ser literalmente igual a la real —
    esto es justo lo que produjo el bug que motivó esta función."""
    settings = _settings()
    assert _resolve_test_database_url(settings) != settings.database_url


def test_explicit_test_database_url_overrides_derivation():
    settings = _settings(
        test_database_url="postgresql+psycopg://crypto:crypto@ci-host:5432/ci_test_db"
    )
    assert _resolve_test_database_url(settings) == "postgresql+psycopg://crypto:crypto@ci-host:5432/ci_test_db"


def test_preserves_user_host_and_port_when_deriving():
    settings = _settings(
        database_url="postgresql+psycopg://otro_user:pw@192.168.1.5:5433/otra_base"
    )
    result = _resolve_test_database_url(settings)
    assert result == "postgresql+psycopg://otro_user:pw@192.168.1.5:5433/otra_base_test"
