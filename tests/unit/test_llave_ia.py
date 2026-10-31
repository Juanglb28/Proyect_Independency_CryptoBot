"""Verificación previa de la llave de IA (decisión del 2026-10-30, tras la llave vencida)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx
import pytest

from app.ai.client import LlaveIARechazada, verificar_llave
from app.domain.enums import TipoAlerta
from app.domain.rules.ai_budget import mensaje_llave_ia_rechazada

LLAVE_SECRETA = "sk-ant-api03-ESTA-LLAVE-NO-DEBE-APARECER-EN-NINGUN-MENSAJE"


def _error(clase, codigo: int):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(codigo, request=req, headers={"request-id": "req_123"})
    return clase("invalid x-api-key", response=resp, body=None)


def _settings(llave=LLAVE_SECRETA):
    return SimpleNamespace(anthropic_api_key=llave)


class _ClienteFalso:
    """Sustituye a `anthropic.Anthropic`: registra cómo se construyó y qué se pidió."""

    construido: dict = {}
    pedido: dict = {}
    error: Exception | None = None

    def __init__(self, **kwargs):
        type(self).construido = kwargs
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        type(self).pedido = kwargs
        if type(self).error is not None:
            raise type(self).error
        return SimpleNamespace()


@pytest.fixture
def cliente():
    _ClienteFalso.construido, _ClienteFalso.pedido, _ClienteFalso.error = {}, {}, None
    with patch("app.ai.client.anthropic.Anthropic", _ClienteFalso):
        yield _ClienteFalso


class TestVerificarLlave:
    def test_llave_aceptada_no_lanza_y_la_llamada_es_minima(self, cliente):
        assert verificar_llave(_settings(), model="claude-haiku-4-5-20251001") is None
        assert cliente.pedido["max_tokens"] == 1
        assert cliente.pedido["model"] == "claude-haiku-4-5-20251001"
        assert len(cliente.pedido["messages"]) == 1
        # sin reintentos y con tiempo límite corto: no puede colgar el ciclo
        assert cliente.construido["max_retries"] == 0
        assert 0 < cliente.construido["timeout"] <= 60

    @pytest.mark.parametrize("clase, codigo", [(anthropic.AuthenticationError, 401), (anthropic.PermissionDeniedError, 403)])
    def test_401_y_403_se_reportan_como_llave_rechazada_sin_la_llave(self, cliente, clase, codigo):
        cliente.error = _error(clase, codigo)
        with pytest.raises(LlaveIARechazada) as exc:
            verificar_llave(_settings(), model="m")
        texto = str(exc.value)
        assert str(codigo) in texto and "req_123" in texto
        assert LLAVE_SECRETA not in texto
        assert exc.value.__cause__ is None and exc.value.__suppress_context__

    @pytest.mark.parametrize("llave", [None, ""])
    def test_sin_llave_configurada_es_rechazo_sin_llamar_a_la_red(self, cliente, llave):
        with pytest.raises(LlaveIARechazada):
            verificar_llave(_settings(llave), model="m")
        assert cliente.pedido == {}

    def test_un_fallo_que_no_es_de_autenticacion_se_propaga_tal_cual(self, cliente):
        cliente.error = _error(anthropic.InternalServerError, 500)
        with pytest.raises(anthropic.InternalServerError):
            verificar_llave(_settings(), model="m")
        cliente.error = _error(anthropic.RateLimitError, 429)
        with pytest.raises(anthropic.RateLimitError):
            verificar_llave(_settings(), model="m")


class TestMensaje:
    def test_explica_que_hacer_y_nunca_incluye_secretos(self):
        m = mensaje_llave_ia_rechazada(date(2026, 10, 31), detalle="Anthropic rechazó la llave (HTTP 401)")
        for fragmento in ("LLAVE DE IA RECHAZADA", "2026-10-31", "ANTHROPIC_API_KEY", "expiración",
                          "force-recreate scheduler", "El monitor y los stops siguen"):
            assert fragmento in m
        assert "sk-ant" not in m

    def test_el_tipo_de_alerta_cabe_en_la_columna(self):
        assert TipoAlerta.LLAVE_IA_RECHAZADA.value == "LLAVE_IA_RECHAZADA"
        assert len(TipoAlerta.LLAVE_IA_RECHAZADA.value) <= 32  # alertas.tipo es VARCHAR(32)
