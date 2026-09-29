"""Tests de app.ai.client: parseo de JSON de la respuesta y validaciones
básicas del cliente (sección 15.8 del plan, nivel "contrato").

No llama a la API real de Anthropic: eso costaría dinero y requeriría
ANTHROPIC_API_KEY (sección 15.9: "una ejecución de tests limpia pasa sin
depender de... Anthropic"). Una llamada real es responsabilidad de un
script manual (`scripts/run_agent_demo.py`), no de la suite automática.
"""

import pytest

from app.ai.client import AgentResponseError, call_agent, parse_agent_json
from app.config import Settings


def _settings(**overrides) -> Settings:
    base = {"database_url": "postgresql+psycopg://x:x@localhost/x"}
    base.update(overrides)
    return Settings(**base)


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


def test_call_agent_raises_clear_error_without_api_key():
    settings = _settings(anthropic_api_key=None)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        call_agent(settings, model="claude-haiku-4-5-20251001", system_prompt="x", user_content="y")
