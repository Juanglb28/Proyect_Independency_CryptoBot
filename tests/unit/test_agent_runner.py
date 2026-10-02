"""Test de app.ai.agents._runner: la alerta temprana de uso de tokens
cerca del límite (sección 24 del plan — nunca un fallo silencioso), no la
orquestación completa de cada agente (ya cubierta, por agente, en
test_ai_agents.py / test_consolidator_agent.py).
"""

from __future__ import annotations

import json
import logging
import uuid

from pydantic import BaseModel

from app.ai.agents._runner import run_json_agent
from app.ai.client import AgentCallResult
from app.ai.models import MODEL_ANALISTA
from app.config import Settings


class _DummySchema(BaseModel):
    ok: bool


def _settings() -> Settings:
    return Settings(database_url="postgresql+psycopg://x:x@localhost/x", anthropic_api_key="sk-test")


def _patch_call_agent(monkeypatch, output_tokens: int):
    def fake_call_agent(settings, *, model, system_prompt, user_content, max_tokens=4096):
        return AgentCallResult(raw_text=json.dumps({"ok": True}), input_tokens=50, output_tokens=output_tokens)

    monkeypatch.setattr("app.ai.agents._runner.call_agent", fake_call_agent)


def _patch_save_llamada_ia(monkeypatch):
    monkeypatch.setattr("app.ai.agents._runner.save_llamada_ia", lambda session, row: uuid.uuid4())


def _run(monkeypatch, ciclo_id=None):
    return run_json_agent(
        session=object(),
        settings=_settings(),
        agente="agente_de_prueba",
        modelo=MODEL_ANALISTA,
        prompt_version="v1",
        system_prompt="...",
        datos={},
        schema=_DummySchema,
        ciclo_id=ciclo_id,
    )


def test_advierte_cuando_la_respuesta_usa_85_por_ciento_o_mas_del_limite(monkeypatch, caplog):
    # Con el default de run_json_agent (max_tokens=4096), 3500 tokens de
    # salida es ~85.4% — debe disparar la alerta, aunque la respuesta haya
    # terminado bien (no es el caso de corte de call_agent/AgentResponseError).
    _patch_call_agent(monkeypatch, output_tokens=3500)
    _patch_save_llamada_ia(monkeypatch)
    ciclo_id = uuid.uuid4()

    with caplog.at_level(logging.WARNING, logger="app.ai.agents._runner"):
        _run(monkeypatch, ciclo_id=ciclo_id)

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == "WARNING"
    assert record.agente == "agente_de_prueba"
    assert record.modelo == MODEL_ANALISTA
    assert record.output_tokens == 3500
    assert record.max_tokens == 4096
    assert record.ciclo_id == str(ciclo_id)


def test_no_advierte_cuando_la_respuesta_usa_bastante_menos_del_limite(monkeypatch, caplog):
    _patch_call_agent(monkeypatch, output_tokens=1000)
    _patch_save_llamada_ia(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.ai.agents._runner"):
        _run(monkeypatch)

    assert len(caplog.records) == 0


def test_no_advierte_justo_por_debajo_del_umbral(monkeypatch, caplog):
    # 3400 / 4096 ~= 83.0%, por debajo del 85% — caso límite para confirmar
    # que el umbral no se disparó por redondeo.
    _patch_call_agent(monkeypatch, output_tokens=3400)
    _patch_save_llamada_ia(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.ai.agents._runner"):
        _run(monkeypatch)

    assert len(caplog.records) == 0
