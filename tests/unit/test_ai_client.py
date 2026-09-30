"""Tests de app.ai.client: parseo de JSON de la respuesta y validaciones
básicas del cliente (sección 15.8 del plan, nivel "contrato").

No llama a la API real de Anthropic: eso costaría dinero y requeriría
ANTHROPIC_API_KEY (sección 15.9: "una ejecución de tests limpia pasa sin
depender de... Anthropic"). Una llamada real es responsabilidad de un
script manual (`scripts/run_agent_demo.py`), no de la suite automática.
"""

from types import SimpleNamespace

import pytest

from app.ai.client import AgentResponseError, call_agent, parse_agent_json
from app.config import Settings


def _settings(**overrides) -> Settings:
    base = {"database_url": "postgresql+psycopg://x:x@localhost/x", "anthropic_api_key": "sk-test"}
    base.update(overrides)
    return Settings(**base)


def _fake_anthropic_response(*, text: str, stop_reason: str, input_tokens=100, output_tokens=50):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason=stop_reason,
    )


def _patch_anthropic_client(monkeypatch, response):
    class _FakeMessages:
        def create(self, **kwargs):
            return response

    class _FakeAnthropic:
        def __init__(self, api_key):
            self.messages = _FakeMessages()

    monkeypatch.setattr("app.ai.client.anthropic.Anthropic", _FakeAnthropic)


def test_parse_agent_json_accepts_plain_json():
    parsed = parse_agent_json('{"a": 1, "b": "x"}')
    assert parsed == {"a": 1, "b": "x"}


def test_parse_agent_json_strips_markdown_code_fence():
    raw = '```json\n{"a": 1}\n```'
    assert parse_agent_json(raw) == {"a": 1}


def test_parse_agent_json_strips_bare_code_fence_without_json_tag():
    raw = '```\n{"a": 1}\n```'
    assert parse_agent_json(raw) == {"a": 1}


def test_parse_agent_json_raises_on_invalid_json():
    with pytest.raises(AgentResponseError):
        parse_agent_json("esto no es json")


def test_parse_agent_json_raises_when_top_level_is_not_an_object():
    with pytest.raises(AgentResponseError):
        parse_agent_json("[1, 2, 3]")


def test_parse_agent_json_ignores_trailing_text_after_a_complete_json_object(caplog):
    # Visto en la práctica: Haiku 4.5 respondiendo al Auditor con el JSON
    # completo y luego una nota aparte, a pesar de que el prompt pide "solo
    # JSON, sin texto adicional".
    raw = '{"a": 1}\n\nNota: esto no debería estar aquí.'
    with caplog.at_level("WARNING"):
        parsed = parse_agent_json(raw)

    assert parsed == {"a": 1}
    assert "texto adicional" in caplog.text


def test_parse_agent_json_raises_on_invalid_json_includes_raw_text_snippet():
    with pytest.raises(AgentResponseError, match="esto no es json"):
        parse_agent_json("esto no es json")


def test_call_agent_raises_clear_error_without_api_key():
    settings = _settings(anthropic_api_key=None)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        call_agent(settings, model="claude-haiku-4-5-20251001", system_prompt="x", user_content="y")


def test_call_agent_returns_text_when_stop_reason_is_end_turn(monkeypatch):
    _patch_anthropic_client(
        monkeypatch, _fake_anthropic_response(text='{"a": 1}', stop_reason="end_turn")
    )

    result = call_agent(_settings(), model="claude-haiku-4-5-20251001", system_prompt="x", user_content="y")

    assert result.raw_text == '{"a": 1}'
    assert result.input_tokens == 100
    assert result.output_tokens == 50


def test_call_agent_raises_a_clear_error_when_truncated_by_max_tokens(monkeypatch):
    # Bug real (2026-09-30): con varios candidatos, la respuesta del
    # Consolidador se cortó a mitad de una cadena JSON porque nadie
    # revisaba `stop_reason` — el error que llegaba hasta el orquestador
    # era un genérico "Unterminated string" de json.JSONDecoder, sin dejar
    # claro que la causa real fue quedarse sin max_tokens. Ver
    # app.ai.agents.consolidator.MAX_TOKENS_CONSOLIDADOR.
    texto_cortado = '{"recomendacion": "invertir", "opciones": [{"tesis": "el precio sub'
    _patch_anthropic_client(
        monkeypatch,
        _fake_anthropic_response(text=texto_cortado, stop_reason="max_tokens", output_tokens=4096),
    )

    with pytest.raises(AgentResponseError, match="max_tokens=4096"):
        call_agent(_settings(), model="claude-opus-5-5", system_prompt="x", user_content="y")
