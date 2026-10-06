"""`scripts.migrate_all.bases_a_migrar`: qué bases se migran, sin tocar ninguna."""

from __future__ import annotations

from app.config import get_settings
from scripts.migrate_all import bases_a_migrar
from tests.conftest import _resolve_test_database_url

URL = "postgresql+psycopg://u:p@h:5432/crypto_trading"


def test_deriva_la_base_de_pruebas_con_sufijo_test():
    s = get_settings().model_copy(update={"database_url": URL, "test_database_url": None})
    assert bases_a_migrar(s) == [("real", URL), ("pruebas", "postgresql+psycopg://u:p@h:5432/crypto_trading_test")]


def test_respeta_test_database_url_si_esta_definida():
    explicita = "postgresql+psycopg://u:p@h:5432/otra_base"
    s = get_settings().model_copy(update={"database_url": URL, "test_database_url": explicita})
    assert bases_a_migrar(s)[1] == ("pruebas", explicita)


def test_misma_regla_que_los_tests():
    for test_url in (None, "postgresql+psycopg://u:p@h:5432/otra_base"):
        s = get_settings().model_copy(update={"database_url": URL, "test_database_url": test_url})
        assert bases_a_migrar(s)[1][1] == _resolve_test_database_url(s)
