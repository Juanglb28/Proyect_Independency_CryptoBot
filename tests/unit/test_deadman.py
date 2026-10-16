"""Tests del ping al dead-man's-switch (app.notifications.deadman)."""

from __future__ import annotations

import urllib.error
from contextlib import contextmanager

from app.notifications.deadman import ping_deadman

URL = "https://hc-ping.example/abc-secreto"


class _Respuesta:
    def read(self) -> bytes:
        return b"OK"


@contextmanager
def _ok(_peticion, timeout=None):
    yield _Respuesta()


def test_url_vacia_o_none_no_hace_nada():
    assert ping_deadman(None) is False
    assert ping_deadman("") is False


def test_ping_exitoso_devuelve_true_y_usa_get():
    vistos = {}

    @contextmanager
    def opener(peticion, timeout=None):
        vistos["method"] = peticion.get_method()
        vistos["url"] = peticion.full_url
        yield _Respuesta()

    assert ping_deadman(URL, opener=opener) is True
    assert vistos["method"] == "GET" and vistos["url"] == URL


def test_fallo_de_red_devuelve_false_sin_lanzar():
    def opener(_peticion, timeout=None):
        raise urllib.error.URLError("sin ruta al host")

    assert ping_deadman(URL, opener=opener) is False


def test_timeout_devuelve_false_sin_lanzar():
    def opener(_peticion, timeout=None):
        raise TimeoutError()

    assert ping_deadman(URL, opener=opener) is False


def test_el_fallo_no_registra_la_url(caplog):
    def opener(_peticion, timeout=None):
        raise OSError("boom")

    with caplog.at_level("WARNING"):
        assert ping_deadman(URL, opener=opener) is False
    texto = "\n".join(r.getMessage() for r in caplog.records)
    assert "abc-secreto" not in texto and URL not in texto
