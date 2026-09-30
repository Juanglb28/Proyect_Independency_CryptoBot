"""Tests de app.orchestration.daily_cycle.run_daily_cycle (Paso 7) contra
Postgres real de pruebas.

Igual que tests/integration/test_consolidator_agent.py, se parchea
`call_agent` (nunca `run_json_agent` ni capas más internas) para no
depender de la API real de Anthropic — pero el pre-filtro (parcheado
aparte, para no depender de que los 20 símbolos de la whitelist tengan
datos suficientes en la base de pruebas), las lecturas de datos por
candidato, la validación de schema, el cálculo del score y la persistencia
SÍ corren de verdad.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.ai.client import AgentCallResult
from app.config import Settings
from app.data.exchange_client import Candle
from app.data.indicator_calculator import IndicatorRow, sma_indicator_name
from app.data.prefilter import PrefilterScore
from app.data.prefilter_pipeline import PrefilterRunResult
from app.domain.enums import EstadoCiclo
from app.orchestration.daily_cycle import run_daily_cycle
from app.repositories.analysis import get_ciclo, get_factores_de_propuesta, get_propuestas_de_ciclo
from app.repositories.candles import save_candles
from app.repositories.indicators import save_indicators

_OPEN_TIME = datetime(2026, 9, 29, tzinfo=timezone.utc)


def _settings() -> Settings:
    return Settings(database_url="postgresql+psycopg://x:x@localhost/x", anthropic_api_key="sk-test")


def _candle(symbol: str) -> Candle:
    return Candle(
        symbol=symbol,
        timeframe="1d",
        open_time=_OPEN_TIME,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("95"),
        close=Decimal("105"),
        volume=Decimal("1000"),
    )


def _indicators(symbol: str) -> list[IndicatorRow]:
    rows = [
        IndicatorRow(symbol, "1d", _OPEN_TIME, "RSI_14", Decimal("55")),
        IndicatorRow(symbol, "1d", _OPEN_TIME, "ATR_14", Decimal("3.0")),
        IndicatorRow(symbol, "1d", _OPEN_TIME, "VOLUMEN_RELATIVO_30", Decimal("1.5")),
        IndicatorRow(symbol, "1d", _OPEN_TIME, "DISTANCIA_ATH", Decimal("-10")),
        IndicatorRow(symbol, "1d", _OPEN_TIME, "DISTANCIA_MAX_90D", Decimal("-3")),
    ]
    for period in (90, 120, 180):
        rows.append(IndicatorRow(symbol, "1d", _OPEN_TIME, sma_indicator_name(period), Decimal("100")))
    return rows


def _seed_symbol(session, symbol: str) -> None:
    save_candles(session, [_candle(symbol)])
    save_indicators(session, _indicators(symbol))


def _fake_prefilter(symbols: list[str]):
    def fake(session):
        candidates = tuple(
            PrefilterScore(
                symbol=symbol,
                calculated_at=_OPEN_TIME,
                tendencia_pct=Decimal("5"),
                volumen_relativo=Decimal("1.5"),
                distancia_max_90d_pct=Decimal("-3"),
                score_tendencia=Decimal("60"),
                score_volumen=Decimal("60"),
                score_distancia=Decimal("60"),
                score_final=Decimal("60"),
            )
            for symbol in symbols
        )
        return PrefilterRunResult(scores=candidates, candidates=candidates, symbols_sin_datos=())

    return fake


def _make_fake_call_agent(calidad_por_simbolo: dict[str, str]):
    """`calidad_por_simbolo[symbol]` controla la `calidad_global` que el
    Auditor "reporta" para ese símbolo — el resto de la respuesta de cada
    agente es fija, suficiente para pasar los 4 schemas."""

    def fake_call_agent(settings, *, model, system_prompt, user_content, max_tokens=4096):
        datos = json.loads(user_content)

        if "analista cuantitativo" in system_prompt:
            resp = {
                "simbolo": datos["simbolo"],
                "observaciones": [],
                "datos_faltantes": [],
                "conflictos_entre_temporalidades": "ninguno detectado",
                "confianza": "alta",
                "justificacion_confianza": "datos completos para este test",
            }
        elif "analista de contexto macro" in system_prompt:
            resp = {
                "simbolo": datos["simbolo"],
                "eventos_comparables": [],
                "tamano_muestra": 0,
                "consistencia_historica": "mixta",
                "lectura_general": "sin eventos comparables en este test",
                "confianza": "media",
                "anomalias_detectadas": [],
            }
        elif "auditor de calidad de datos" in system_prompt:
            calidad = calidad_por_simbolo.get(datos["simbolo"], "suficiente")
            resp = {
                "campos_faltantes": [],
                "huecos_en_series": [],
                "valores_anomalos": [],
                "antiguedad_maxima_horas": 5.0,
                "calidad_global": calidad,
                "penalizacion_confianza_pct": 50.0 if calidad == "insuficiente" else 0.0,
                "bloqueantes": ["datos insuficientes para este test"] if calidad == "insuficiente" else [],
            }
        elif "consolidador de análisis" in system_prompt:
            candidatos = datos["candidatos"]
            opciones = [
                {
                    "simbolo": c["simbolo"],
                    "categoria": "principal",
                    "horizonte": "corto",
                    "nivel_riesgo": "medio",
                    "tendencia_momentum": 70,
                    "volumen_liquidez": 60,
                    "posicion_rangos_ath": 50,
                    "contexto_eventos": 80,
                    "tesis": "tesis de prueba",
                    "invalidacion": "invalidación de prueba",
                    "escenario_adverso": {
                        "descripcion": "reversión de prueba",
                        "desencadenante": "gatillo de prueba",
                        "probabilidad_relativa": "baja",
                    },
                    "comentario_chartista": None,
                    "fuentes": [],
                }
                for c in candidatos
            ]
            resp = {
                "recomendacion": "invertir" if opciones else "no_invertir",
                "razon_no_invertir": None if opciones else "sin candidatos que sostengan una tesis",
                "opciones": opciones,
                "confianza_global": "alta",
            }
        else:
            raise AssertionError(f"system_prompt no reconocido en el fake: {system_prompt[:80]!r}")

        return AgentCallResult(raw_text=json.dumps(resp), input_tokens=100, output_tokens=100)

    return fake_call_agent


def test_sin_candidatos_el_ciclo_termina_sin_ninguna_llamada(monkeypatch, db_session):
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter([]))

    def _fail_si_se_llama(*args, **kwargs):
        raise AssertionError("no debería llamarse a ningún agente sin candidatos")

    monkeypatch.setattr("app.ai.agents._runner.call_agent", _fail_si_se_llama)

    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)

    assert result.propuesta_ids == ()
    assert result.cost_usd == Decimal(0)
    ciclo = get_ciclo(db_session, result.ciclo_id)
    assert ciclo.status == EstadoCiclo.PREFILTER
    assert ciclo.finished_at is not None
    assert ciclo.error_message is None


def test_ciclo_completo_con_dos_candidatos_persiste_propuestas_y_factores(monkeypatch, db_session):
    _seed_symbol(db_session, "BTCUSDT")
    _seed_symbol(db_session, "ETHUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["BTCUSDT", "ETHUSDT"]))
    monkeypatch.setattr(
        "app.ai.agents._runner.call_agent",
        _make_fake_call_agent({"BTCUSDT": "suficiente", "ETHUSDT": "suficiente"}),
    )

    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)

    assert set(result.candidatos_evaluados) == {"BTCUSDT", "ETHUSDT"}
    assert result.candidatos_excluidos_por_calidad == ()
    assert len(result.propuesta_ids) == 2
    assert result.cost_usd > 0

    ciclo = get_ciclo(db_session, result.ciclo_id)
    assert ciclo.status == EstadoCiclo.FREEZE_DECISION_SNAPSHOT
    assert ciclo.error_message is None
    assert ciclo.cost_usd == result.cost_usd

    propuestas = get_propuestas_de_ciclo(db_session, result.ciclo_id)
    assert {p.symbol for p in propuestas} == {"BTCUSDT", "ETHUSDT"}
    for propuesta in propuestas:
        assert propuesta.supera_umbral_entrada is True  # 70*.3+60*.2+50*.2+80*.3=67 * calidad 1.0
        assert propuesta.decision_snapshot["symbol"] == propuesta.symbol
        assert propuesta.decision_snapshot["analysis_cycle_id"] == str(result.ciclo_id)
        assert "initial_stop" not in propuesta.decision_snapshot
        assert "initial_target" not in propuesta.decision_snapshot

        factores = get_factores_de_propuesta(db_session, propuesta.id)
        assert len(factores) == 4
        assert {f.factor for f in factores} == {
            "tendencia_momentum",
            "volumen_liquidez",
            "posicion_rangos_ath",
            "contexto_eventos",
        }
        assert all(f.confianza == "alta" for f in factores)  # confianza_global del Consolidador


def test_candidato_con_calidad_insuficiente_se_excluye_del_consolidador(monkeypatch, db_session):
    _seed_symbol(db_session, "BTCUSDT")
    _seed_symbol(db_session, "ETHUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["BTCUSDT", "ETHUSDT"]))
    monkeypatch.setattr(
        "app.ai.agents._runner.call_agent",
        _make_fake_call_agent({"BTCUSDT": "insuficiente", "ETHUSDT": "suficiente"}),
    )

    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)

    assert result.candidatos_excluidos_por_calidad == ("BTCUSDT",)
    propuestas = get_propuestas_de_ciclo(db_session, result.ciclo_id)
    assert {p.symbol for p in propuestas} == {"ETHUSDT"}


def test_todos_los_candidatos_insuficientes_termina_sin_llamar_al_consolidador(monkeypatch, db_session):
    _seed_symbol(db_session, "BTCUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["BTCUSDT"]))

    llamadas_consolidador: list[str] = []

    def fake_call_agent(settings, *, model, system_prompt, user_content, max_tokens=4096):
        if "consolidador de análisis" in system_prompt:
            llamadas_consolidador.append(system_prompt)
        return _make_fake_call_agent({"BTCUSDT": "insuficiente"})(
            settings, model=model, system_prompt=system_prompt, user_content=user_content, max_tokens=max_tokens
        )

    monkeypatch.setattr("app.ai.agents._runner.call_agent", fake_call_agent)

    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)

    assert llamadas_consolidador == []
    assert result.propuesta_ids == ()
    assert result.candidatos_excluidos_por_calidad == ("BTCUSDT",)

    ciclo = get_ciclo(db_session, result.ciclo_id)
    assert ciclo.status == EstadoCiclo.AI_ANALYSIS
    assert ciclo.error_message is None


def test_error_en_un_agente_marca_el_ciclo_como_fallido_y_relanza(monkeypatch, db_session):
    _seed_symbol(db_session, "BTCUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["BTCUSDT"]))

    def fake_call_agent_roto(settings, *, model, system_prompt, user_content, max_tokens=4096):
        raise RuntimeError("la API de Anthropic no respondió (simulado)")

    monkeypatch.setattr("app.ai.agents._runner.call_agent", fake_call_agent_roto)

    with pytest.raises(RuntimeError, match="no respondió"):
        run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)

    # El ciclo debe quedar registrado como fallido en la ÚLTIMA etapa
    # alcanzada (sección 15.6: nunca saltar en silencio a una etapa
    # posterior) — no debe desaparecer ni quedar en un estado ambiguo.
    from sqlalchemy import select

    from app.db.models.analysis import CicloAnalisis

    ciclo = db_session.scalars(
        select(CicloAnalisis).order_by(CicloAnalisis.created_at.desc()).limit(1)
    ).first()
    assert ciclo is not None
    assert ciclo.status == EstadoCiclo.AI_ANALYSIS
    assert ciclo.error_message is not None
    assert "no respondió" in ciclo.error_message
    assert ciclo.finished_at is not None
