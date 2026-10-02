"""Test de app.ai.agents._runner: la alerta temprana de uso de tokens
cerca del límite (sección 24 del plan — nunca un fallo silencioso) y el
registro de costo de llamadas fallidas (bug real 2026-10-02 — Anthropic
las cobra igual). No cubre la orquestación completa de cada agente (ya
cubierta, por agente, en test_ai_agents.py / test_consolidator_agent.py).
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from pydantic import BaseModel

from app.ai.agents._runner import AgentValidationError, run_json_agent
from app.ai.client import AgentCallResult, AgentResponseError
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


def _patch_call_agent_devuelve(monkeypatch, *, raw_text: str, input_tokens=100, output_tokens=200):
    def fake_call_agent(settings, *, model, system_prompt, user_content, max_tokens=4096):
        return AgentCallResult(
            raw_text=raw_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=10,
            cache_read_input_tokens=5,
        )

    monkeypatch.setattr("app.ai.agents._runner.call_agent", fake_call_agent)


def _patch_call_agent_se_corta_por_max_tokens(monkeypatch, *, output_tokens=4096):
    tokens = AgentCallResult(
        raw_text="{incompleto",
        input_tokens=100,
        output_tokens=output_tokens,
        cache_creation_input_tokens=10,
        cache_read_input_tokens=5,
    )

    def fake_call_agent(settings, *, model, system_prompt, user_content, max_tokens=4096):
        raise AgentResponseError("se cortó por max_tokens", tokens=tokens)

    monkeypatch.setattr("app.ai.agents._runner.call_agent", fake_call_agent)


def _patch_save_llamada_ia(monkeypatch, capture: list | None = None):
    def fake_save(session, row):
        if capture is not None:
            capture.append(row)
        return uuid.uuid4()

    monkeypatch.setattr("app.ai.agents._runner.save_llamada_ia", fake_save)


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


# ---------------------------------------------------------------------------
# Registro de costo de llamadas fallidas (bug real 2026-10-02)
# ---------------------------------------------------------------------------


def test_llamada_truncada_por_max_tokens_persiste_su_costo_real_y_relanza(monkeypatch):
    # call_agent detecta el corte ANTES de devolver un AgentCallResult, así
    # que los tokens viajan en AgentResponseError.tokens (ver
    # app/ai/client.py) — este es el caso que más dinero puede perder si no
    # se registra: el agente ya generó hasta max_tokens de salida.
    _patch_call_agent_se_corta_por_max_tokens(monkeypatch, output_tokens=4096)
    capturado: list = []
    _patch_save_llamada_ia(monkeypatch, capture=capturado)

    with pytest.raises(AgentResponseError):
        _run(monkeypatch)

    assert len(capturado) == 1
    row = capturado[0]
    assert row.exitosa is False
    assert row.respuesta is None
    assert row.output_tokens == 4096
    assert row.cost_usd > 0
    assert "max_tokens" in row.error_message


def test_llamada_con_json_invalido_persiste_su_costo_real_y_relanza(monkeypatch):
    # Acá call_agent SÍ devuelve un AgentCallResult completo (la API
    # respondió, solo que el texto no es JSON válido) — los tokens deben
    # salir de ese call_result, no de la excepción (que no los trae).
    _patch_call_agent_devuelve(monkeypatch, raw_text="esto no es json")
    capturado: list = []
    _patch_save_llamada_ia(monkeypatch, capture=capturado)

    with pytest.raises(AgentResponseError):
        _run(monkeypatch)

    assert len(capturado) == 1
    row = capturado[0]
    assert row.exitosa is False
    assert row.respuesta is None
    assert row.input_tokens == 100
    assert row.output_tokens == 200
    assert row.cost_usd > 0


def test_llamada_que_no_cumple_el_schema_persiste_su_costo_real_y_relanza(monkeypatch):
    # JSON válido, pero le falta el campo "ok" que exige _DummySchema.
    _patch_call_agent_devuelve(monkeypatch, raw_text="{}")
    capturado: list = []
    _patch_save_llamada_ia(monkeypatch, capture=capturado)

    with pytest.raises(AgentValidationError):
        _run(monkeypatch)

    assert len(capturado) == 1
    row = capturado[0]
    assert row.exitosa is False
    assert row.respuesta is None
    assert row.cost_usd > 0
    assert "_DummySchema" in row.error_message or "DummySchema" in row.error_message


def test_llamada_exitosa_no_registra_ninguna_fila_de_fallo(monkeypatch):
    # Caso de control: una llamada exitosa solo persiste UNA fila, y es la
    # exitosa (exitosa=True) — no debe quedar ningún registro de fallo.
    _patch_call_agent(monkeypatch, output_tokens=50)
    capturado: list = []
    _patch_save_llamada_ia(monkeypatch, capture=capturado)

    _run(monkeypatch)

    assert len(capturado) == 1
    assert capturado[0].exitosa is True
    assert capturado[0].respuesta == {"ok": True}
