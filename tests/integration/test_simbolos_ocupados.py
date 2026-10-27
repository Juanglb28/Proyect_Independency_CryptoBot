"""Símbolos ocupados (posición abierta o entrada pendiente) y su efecto en el pre-filtro
y en el ciclo diario (decisión del 2026-10-26: el sistema no aumenta posiciones, así que
un símbolo ocupado no se analiza con IA)."""

from __future__ import annotations

import json

from app.domain.enums import EstadoPrefiltro, TierEntrada
from app.execution.order_manager import build_cupos, open_entries, reject_pending_entry
from app.orchestration.daily_cycle import run_daily_cycle
from app.repositories.analysis import get_propuestas_de_ciclo, list_prefiltro_de_ciclo
from app.repositories.simbolos_ocupados import simbolos_ocupados
from tests.integration.test_daily_cycle import (
    _DQ_PATCH_TARGET,
    _OPEN_TIME,
    _fake_prefilter,
    _make_fake_call_agent,
    _perfect_data_quality,
    _seed_symbol,
    _settings,
)
from tests.integration.test_order_manager import FakePrices, _AHORA, _candidato, _ciclo


def _abrir_posicion(db_session, symbol: str) -> None:
    cand = _candidato(db_session, _ciclo(db_session), symbol)
    open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({symbol: "100"}))


def _dejar_entrada_pendiente(db_session, symbol: str):
    cand = _candidato(db_session, _ciclo(db_session), symbol, TierEntrada.MAYOR_RIESGO, "52")
    return open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({})).procesadas[0].orden_id


# --- el repositorio ---


def test_sin_posiciones_ni_entradas_no_hay_simbolos_ocupados(db_session):
    assert simbolos_ocupados(db_session) == frozenset()


def test_una_posicion_abierta_ocupa_su_simbolo(db_session):
    _abrir_posicion(db_session, "BTCUSDT")
    assert simbolos_ocupados(db_session) == frozenset({"BTCUSDT"})


def test_una_entrada_pendiente_ocupa_su_simbolo(db_session):
    _dejar_entrada_pendiente(db_session, "ETHUSDT")
    assert simbolos_ocupados(db_session) == frozenset({"ETHUSDT"})


def test_una_entrada_rechazada_libera_el_simbolo(db_session):
    orden_id = _dejar_entrada_pendiente(db_session, "ETHUSDT")
    assert reject_pending_entry(db_session, orden_id, now=_AHORA) is True
    assert simbolos_ocupados(db_session) == frozenset()


def test_es_la_misma_definicion_que_usa_la_validacion_de_riesgo(db_session):
    """`build_cupos` (ejecución) y `simbolos_ocupados` (pre-filtro) no pueden diverger: si lo
    hicieran, el pre-filtro dejaría pasar a un símbolo que el riesgo igual rechaza, o al revés."""
    assert simbolos_ocupados(db_session) == build_cupos(db_session, _AHORA).simbolos_abiertos
    _abrir_posicion(db_session, "BTCUSDT")
    _dejar_entrada_pendiente(db_session, "ETHUSDT")
    assert simbolos_ocupados(db_session) == build_cupos(db_session, _AHORA).simbolos_abiertos == frozenset(
        {"BTCUSDT", "ETHUSDT"}
    )


# --- el ciclo diario ---


def test_un_simbolo_con_posicion_abierta_no_se_analiza_con_ia(monkeypatch, db_session):
    _abrir_posicion(db_session, "BTCUSDT")
    _seed_symbol(db_session, "BTCUSDT")
    _seed_symbol(db_session, "ETHUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["BTCUSDT", "ETHUSDT"]))
    monkeypatch.setattr(_DQ_PATCH_TARGET, _perfect_data_quality)

    base = _make_fake_call_agent({"ETHUSDT": "suficiente"})
    simbolos_llamados: list[str] = []

    def espia(settings, *, model, system_prompt, user_content, max_tokens=4096):
        s = json.loads(user_content).get("simbolo")
        if s is not None:
            simbolos_llamados.append(s)
        return base(settings, model=model, system_prompt=system_prompt, user_content=user_content, max_tokens=max_tokens)

    monkeypatch.setattr("app.ai.agents._runner.call_agent", espia)

    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)

    assert result.candidatos_evaluados == ("ETHUSDT",)
    assert set(simbolos_llamados) == {"ETHUSDT"}  # a BTCUSDT no se le llamó a ningún agente
    assert {p.symbol for p in get_propuestas_de_ciclo(db_session, result.ciclo_id)} == {"ETHUSDT"}

    # La evidencia queda registrada: no es un candidato que "desapareció".
    estados = {r.symbol: r.estado for r in list_prefiltro_de_ciclo(db_session, result.ciclo_id)}
    assert estados == {
        "BTCUSDT": EstadoPrefiltro.DESCARTADO_POR_POSICION_ABIERTA,
        "ETHUSDT": EstadoPrefiltro.CANDIDATO,
    }


def test_si_todos_los_candidatos_estan_ocupados_el_ciclo_termina_sin_llamar_a_la_ia(monkeypatch, db_session):
    _abrir_posicion(db_session, "BTCUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["BTCUSDT"]))

    def _fail_si_se_llama(*args, **kwargs):
        raise AssertionError("no debería llamarse a ningún agente: el único candidato ya tiene posición abierta")

    monkeypatch.setattr("app.ai.agents._runner.call_agent", _fail_si_se_llama)

    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)

    assert result.candidatos_evaluados == () and result.propuesta_ids == ()
    assert result.cost_usd == 0
    estados = {r.symbol: r.estado for r in list_prefiltro_de_ciclo(db_session, result.ciclo_id)}
    assert estados == {"BTCUSDT": EstadoPrefiltro.DESCARTADO_POR_POSICION_ABIERTA}


def test_sin_posiciones_el_ciclo_analiza_a_todos_los_candidatos_como_siempre(monkeypatch, db_session):
    _seed_symbol(db_session, "BTCUSDT")
    _seed_symbol(db_session, "ETHUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["BTCUSDT", "ETHUSDT"]))
    monkeypatch.setattr(
        "app.ai.agents._runner.call_agent", _make_fake_call_agent({"BTCUSDT": "suficiente", "ETHUSDT": "suficiente"})
    )
    monkeypatch.setattr(_DQ_PATCH_TARGET, _perfect_data_quality)

    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)

    assert set(result.candidatos_evaluados) == {"BTCUSDT", "ETHUSDT"}
    estados = {r.symbol: r.estado for r in list_prefiltro_de_ciclo(db_session, result.ciclo_id)}
    assert estados == {"BTCUSDT": EstadoPrefiltro.CANDIDATO, "ETHUSDT": EstadoPrefiltro.CANDIDATO}
