"""Test de integración de app.ai.agents.consolidator.run_consolidador.

A diferencia de tests/unit/test_ai_agents.py (que cubre a los agentes 2/3/4
con `session=object()` porque nunca tocan la base de datos), el Consolidador
sí hace una consulta real dentro de `_validar_fuentes`
(`get_existing_external_ids`) para confirmar que "fuentes" nunca referencie
un evento inventado (sección P3 del plan). Por eso este test usa `db_session`
(Postgres real de pruebas) en vez de un doble — pero sigue parcheando
`call_agent`/`save_llamada_ia`, igual que test_ai_agents.py, para no
depender de la API de Anthropic real.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.ai.agents._runner import AgentValidationError
from app.ai.agents.consolidator import run_consolidador
from app.ai.client import AgentCallResult
from app.config import Settings
from app.repositories.events import EventoInput, save_events


def _settings() -> Settings:
    return Settings(database_url="postgresql+psycopg://x:x@localhost/x", anthropic_api_key="sk-test")


def _patch_call_agent(monkeypatch, raw_text: str, input_tokens=200, output_tokens=150):
    def fake_call_agent(settings, *, model, system_prompt, user_content, max_tokens=4096):
        return AgentCallResult(raw_text=raw_text, input_tokens=input_tokens, output_tokens=output_tokens)

    monkeypatch.setattr("app.ai.agents._runner.call_agent", fake_call_agent)


def _patch_save_llamada_ia(monkeypatch):
    saved: list[object] = []

    def fake_save(session, row):
        saved.append(row)
        return uuid.uuid4()

    monkeypatch.setattr("app.ai.agents._runner.save_llamada_ia", fake_save)
    return saved


def _opcion(**overrides) -> dict:
    defaults = dict(
        simbolo="AVAXUSDT",
        categoria="principal",
        horizonte="corto",
        nivel_riesgo="medio",
        tendencia_momentum=82,
        volumen_liquidez=82,
        posicion_rangos_ath=82,
        contexto_eventos=82,
        tesis="tendencia consistente en 4 temporalidades con volumen relativo alto",
        invalidacion="pérdida persistente de momentum relativo acompañada de deterioro de liquidez",
        escenario_adverso={
            "descripcion": "reversión brusca tras agotamiento de la tendencia",
            "desencadenante": "caída de volumen relativo por debajo de 1x",
            "probabilidad_relativa": "media",
        },
        comentario_chartista=None,
        fuentes=[],
    )
    defaults.update(overrides)
    return defaults


def test_run_consolidador_validates_scores_and_persists(monkeypatch, db_session):
    respuesta = {
        "recomendacion": "invertir",
        "razon_no_invertir": None,
        "opciones": [_opcion()],
        "confianza_global": "media",
    }
    _patch_call_agent(monkeypatch, json.dumps(respuesta))
    saved = _patch_save_llamada_ia(monkeypatch)

    result = run_consolidador(
        db_session,
        _settings(),
        datos={"candidatos": ["AVAXUSDT"]},
        calidad_datos_por_simbolo={"AVAXUSDT": Decimal(70)},
    )

    assert result.output.recomendacion == "invertir"
    assert len(result.opciones_evaluadas) == 1

    evaluada = result.opciones_evaluadas[0]
    # Coincide con el ejemplo trabajado a mano durante el diseño: 82 de
    # puntaje base * 0.70 de calidad de datos = 57.4.
    assert evaluada.score.puntaje_base == Decimal("82.00")
    assert evaluada.score.puntaje_ponderado == Decimal("57.400")
    assert evaluada.score.supera_umbral_entrada is True

    assert len(saved) == 1
    assert saved[0].agente == "consolidador"
    assert saved[0].modelo == "claude-opus-5-5"


def test_run_consolidador_accepts_fuente_that_exists_in_catalog(monkeypatch, db_session):
    save_events(
        db_session,
        [
            EventoInput(
                external_id="ripple-sec-case-ends",
                event_date=date(2024, 1, 1),
                event_type="REGULATORIO",
                description="fin del caso SEC vs. Ripple",
            )
        ],
    )
    respuesta = {
        "recomendacion": "invertir",
        "razon_no_invertir": None,
        "opciones": [_opcion(fuentes=["ripple-sec-case-ends"])],
        "confianza_global": "media",
    }
    _patch_call_agent(monkeypatch, json.dumps(respuesta))
    _patch_save_llamada_ia(monkeypatch)

    result = run_consolidador(
        db_session,
        _settings(),
        datos={},
        calidad_datos_por_simbolo={"AVAXUSDT": Decimal(70)},
    )

    assert result.output.opciones[0].fuentes == ["ripple-sec-case-ends"]


def test_run_consolidador_rejects_fuente_that_does_not_exist_in_catalog(monkeypatch, db_session):
    respuesta = {
        "recomendacion": "invertir",
        "razon_no_invertir": None,
        "opciones": [_opcion(fuentes=["evento-inventado-por-el-modelo"])],
        "confianza_global": "media",
    }
    _patch_call_agent(monkeypatch, json.dumps(respuesta))
    saved = _patch_save_llamada_ia(monkeypatch)

    with pytest.raises(AgentValidationError):
        run_consolidador(
            db_session,
            _settings(),
            datos={},
            calidad_datos_por_simbolo={"AVAXUSDT": Decimal(70)},
        )

    # Una respuesta que cita un evento inventado tampoco debe quedar
    # registrada como una interpretación válida (mismo criterio que un
    # fallo de schema en tests/unit/test_ai_agents.py).
    assert saved == []


def test_run_consolidador_raises_key_error_when_calidad_datos_missing_for_a_symbol(monkeypatch, db_session):
    respuesta = {
        "recomendacion": "invertir",
        "razon_no_invertir": None,
        "opciones": [_opcion(simbolo="UNIUSDT")],
        "confianza_global": "media",
    }
    _patch_call_agent(monkeypatch, json.dumps(respuesta))
    _patch_save_llamada_ia(monkeypatch)

    # calidad_datos_por_simbolo no incluye "UNIUSDT" — nunca se debe asumir
    # un valor por defecto para un dato faltante (sección P3 del plan).
    with pytest.raises(KeyError):
        run_consolidador(
            db_session,
            _settings(),
            datos={},
            calidad_datos_por_simbolo={"AVAXUSDT": Decimal(70)},
        )


def test_run_consolidador_accepts_no_invertir_with_no_opciones(monkeypatch, db_session):
    respuesta = {
        "recomendacion": "no_invertir",
        "razon_no_invertir": "ningún candidato supera el umbral mínimo de entrada",
        "opciones": [],
        "confianza_global": "alta",
    }
    _patch_call_agent(monkeypatch, json.dumps(respuesta))
    saved = _patch_save_llamada_ia(monkeypatch)

    result = run_consolidador(
        db_session,
        _settings(),
        datos={},
        calidad_datos_por_simbolo={},
    )

    assert result.output.recomendacion == "no_invertir"
    assert result.opciones_evaluadas == ()
    assert len(saved) == 1
