"""Tests de app.ai.agents.{quantitative,events,auditor}: orquestación
completa (llamar, parsear, validar, calcular costo, persistir), con
`call_agent`/`save_llamada_ia` parcheados en vez de una API o base de datos
reales — mismo patrón que tests/unit/test_indicator_pipeline.py.
"""

import json
import uuid
from decimal import Decimal

import pytest

from app.ai.agents._runner import AgentValidationError
from app.ai.agents.auditor import run_auditor_datos
from app.ai.agents.events import MAX_TOKENS_ANALISTA_EVENTOS, run_analista_eventos
from app.ai.agents.quantitative import MAX_TOKENS_ANALISTA_CUANTITATIVO, run_analista_cuantitativo
from app.ai.client import AgentCallResult, AgentResponseError
from app.config import Settings


def _settings() -> Settings:
    return Settings(database_url="postgresql+psycopg://x:x@localhost/x", anthropic_api_key="sk-test")


def _patch_call_agent(
    monkeypatch, module_path: str, raw_text: str, input_tokens=100, output_tokens=50, capture: list | None = None
):
    def fake_call_agent(settings, *, model, system_prompt, user_content, max_tokens=4096):
        if capture is not None:
            capture.append(max_tokens)
        return AgentCallResult(raw_text=raw_text, input_tokens=input_tokens, output_tokens=output_tokens)

    monkeypatch.setattr(f"{module_path}.call_agent", fake_call_agent)


def _patch_save_llamada_ia(monkeypatch):
    saved: list[object] = []

    def fake_save(session, row):
        saved.append(row)
        return uuid.uuid4()

    monkeypatch.setattr("app.ai.agents._runner.save_llamada_ia", fake_save)
    return saved


# ---------------------------------------------------------------------------
# Auditor
# ---------------------------------------------------------------------------


def test_run_auditor_datos_validates_and_persists(monkeypatch):
    respuesta = {
        "campos_faltantes": [],
        "huecos_en_series": [],
        "valores_anomalos": [],
        "antiguedad_maxima_horas": 1.0,
        "calidad_global": "suficiente",
        "penalizacion_confianza_pct": 0,
        "bloqueantes": [],
    }
    _patch_call_agent(monkeypatch, "app.ai.agents._runner", json.dumps(respuesta))
    saved = _patch_save_llamada_ia(monkeypatch)

    result = run_auditor_datos(session=object(), settings=_settings(), datos={"simbolo": "BTCUSDT"})

    assert result.output.calidad_global == "suficiente"
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert len(saved) == 1
    assert saved[0].agente == "auditor_datos"
    assert saved[0].respuesta == respuesta
    assert saved[0].cost_usd == Decimal("0.00035")  # (100*1 + 50*5)/1e6


def test_run_auditor_datos_raises_on_malformed_json(monkeypatch):
    _patch_call_agent(monkeypatch, "app.ai.agents._runner", "esto no es json")
    _patch_save_llamada_ia(monkeypatch)

    with pytest.raises(AgentResponseError):
        run_auditor_datos(session=object(), settings=_settings(), datos={})


def test_run_auditor_datos_raises_on_schema_mismatch(monkeypatch):
    # JSON válido, pero "calidad_global" no es una de las 3 opciones del prompt.
    respuesta = {
        "campos_faltantes": [],
        "huecos_en_series": [],
        "valores_anomalos": [],
        "antiguedad_maxima_horas": 1.0,
        "calidad_global": "excelente",
        "penalizacion_confianza_pct": 0,
        "bloqueantes": [],
    }
    _patch_call_agent(monkeypatch, "app.ai.agents._runner", json.dumps(respuesta))
    saved = _patch_save_llamada_ia(monkeypatch)

    with pytest.raises(AgentValidationError):
        run_auditor_datos(session=object(), settings=_settings(), datos={})

    # Una respuesta que no cumple el schema no debe quedar registrada como
    # si fuera una interpretación válida (respuesta=None) — pero bug real
    # 2026-10-02: Anthropic cobró esta llamada igual, así que SÍ debe
    # quedar un registro de auditoría marcado como fallido.
    assert len(saved) == 1
    assert saved[0].exitosa is False
    assert saved[0].respuesta is None
    assert saved[0].cost_usd > 0


# ---------------------------------------------------------------------------
# Analista cuantitativo
# ---------------------------------------------------------------------------


def test_run_analista_cuantitativo_validates_and_persists(monkeypatch):
    respuesta = {
        "simbolo": "AVAXUSDT",
        "observaciones": [
            {
                "factor": "volumen",
                "lectura": "1.88x su media de 30 días",
                "direccion": "favorable",
                "fuerza": 5,
                "campos_usados": ["volumen_relativo_30_1d"],
            }
        ],
        "datos_faltantes": [],
        "conflictos_entre_temporalidades": "ninguno",
        "confianza": "alta",
        "justificacion_confianza": "señal consistente en las 4 temporalidades",
    }
    _patch_call_agent(monkeypatch, "app.ai.agents._runner", json.dumps(respuesta))
    saved = _patch_save_llamada_ia(monkeypatch)

    result = run_analista_cuantitativo(session=object(), settings=_settings(), datos={"simbolo": "AVAXUSDT"})

    assert result.output.simbolo == "AVAXUSDT"
    assert saved[0].agente == "analista_cuantitativo"
    assert saved[0].modelo == "claude-sonnet-5-5"


def test_run_analista_cuantitativo_pasa_su_propio_max_tokens_al_llamar_al_agente(monkeypatch):
    # Mismo mecanismo que test_run_consolidador_pasa_su_propio_max_tokens_...:
    # confirma que run_analista_cuantitativo propaga MAX_TOKENS_ANALISTA_CUANTITATIVO
    # (preventivo — ver el docstring de la constante) en vez del default de
    # run_json_agent pensado para una respuesta más chica.
    respuesta = {
        "simbolo": "AVAXUSDT",
        "observaciones": [],
        "datos_faltantes": [],
        "conflictos_entre_temporalidades": "ninguno",
        "confianza": "alta",
        "justificacion_confianza": "sin observaciones",
    }
    capturado: list[int] = []
    _patch_call_agent(monkeypatch, "app.ai.agents._runner", json.dumps(respuesta), capture=capturado)
    _patch_save_llamada_ia(monkeypatch)

    run_analista_cuantitativo(session=object(), settings=_settings(), datos={"simbolo": "AVAXUSDT"})

    assert capturado == [MAX_TOKENS_ANALISTA_CUANTITATIVO]


# ---------------------------------------------------------------------------
# Analista de eventos
# ---------------------------------------------------------------------------


def test_run_analista_eventos_validates_and_persists(monkeypatch):
    respuesta = {
        "simbolo": "ADAUSDT",
        "eventos_comparables": [],
        "tamano_muestra": 0,
        "consistencia_historica": "contradictoria",
        "lectura_general": "sin eventos comparables en el catálogo",
        "confianza": "baja",
        "anomalias_detectadas": [],
    }
    _patch_call_agent(monkeypatch, "app.ai.agents._runner", json.dumps(respuesta))
    saved = _patch_save_llamada_ia(monkeypatch)

    result = run_analista_eventos(session=object(), settings=_settings(), datos={"simbolo": "ADAUSDT"})

    assert result.output.confianza == "baja"
    assert saved[0].agente == "analista_eventos"


def test_run_analista_eventos_pasa_su_propio_max_tokens_al_llamar_al_agente(monkeypatch):
    # Bug real (2026-10-02): con el default de run_json_agent (4096), una
    # corrida real con 5-6 eventos en alcance cortó la respuesta a mitad de
    # JSON. Este test no reproduce la API real — solo confirma que
    # run_analista_eventos de verdad propaga MAX_TOKENS_ANALISTA_EVENTOS
    # hacia call_agent, en vez de quedarse con el default pensado para una
    # respuesta sin eventos comparables.
    respuesta = {
        "simbolo": "ADAUSDT",
        "eventos_comparables": [],
        "tamano_muestra": 0,
        "consistencia_historica": "contradictoria",
        "lectura_general": "sin eventos comparables en el catálogo",
        "confianza": "baja",
        "anomalias_detectadas": [],
    }
    capturado: list[int] = []
    _patch_call_agent(monkeypatch, "app.ai.agents._runner", json.dumps(respuesta), capture=capturado)
    _patch_save_llamada_ia(monkeypatch)

    run_analista_eventos(session=object(), settings=_settings(), datos={"simbolo": "ADAUSDT"})

    assert capturado == [MAX_TOKENS_ANALISTA_EVENTOS]
