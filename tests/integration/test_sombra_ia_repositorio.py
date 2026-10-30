"""S1: lectura de lo que la IA decidió, tal como queda guardado por un ciclo diario real
(con agentes simulados). Verifica el repositorio de solo lectura y el armado de unidades."""

from __future__ import annotations

from app.domain.rules.sombra_ia import (
    CicloConConsolidador,
    FilaPrefiltroS1,
    NivelIA,
    PropuestaS1,
    construir_unidades_base,
    contar_muestra,
)
from app.orchestration.daily_cycle import run_daily_cycle
from app.repositories.sombra_ia import leer_ciclos_con_consolidador, leer_prefiltro, leer_propuestas
from tests.integration.test_daily_cycle import (
    _DQ_PATCH_TARGET,
    _OPEN_TIME,
    _blocked_data_quality,
    _fake_prefilter,
    _make_fake_call_agent,
    _perfect_data_quality,
    _seed_symbol,
    _settings,
)


def _convertir(db_session):
    return construir_unidades_base(
        [CicloConConsolidador(**vars(f)) for f in leer_ciclos_con_consolidador(db_session)],
        [FilaPrefiltroS1(**vars(f)) for f in leer_prefiltro(db_session)],
        [PropuestaS1(**vars(f)) for f in leer_propuestas(db_session)],
    )


def test_sin_ciclos_no_hay_unidades(db_session):
    assert leer_ciclos_con_consolidador(db_session) == []
    assert _convertir(db_session) == []


def test_un_ciclo_con_un_incluido_y_un_excluido_por_calidad(monkeypatch, db_session):
    _seed_symbol(db_session, "BTCUSDT")
    _seed_symbol(db_session, "ETHUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["BTCUSDT", "ETHUSDT"]))
    monkeypatch.setattr(
        "app.ai.agents._runner.call_agent",
        _make_fake_call_agent({"BTCUSDT": "suficiente", "ETHUSDT": "suficiente"}),
    )
    llamadas = {"n": 0}

    def _dq_btc_bloqueado(**kwargs):
        llamadas["n"] += 1
        return _blocked_data_quality(**kwargs) if llamadas["n"] == 1 else _perfect_data_quality(**kwargs)

    monkeypatch.setattr(_DQ_PATCH_TARGET, _dq_btc_bloqueado)

    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)

    (ciclo,) = leer_ciclos_con_consolidador(db_session)
    assert ciclo.ciclo_id == str(result.ciclo_id)
    assert ciclo.recomendacion == "invertir"

    por_simbolo = {u.symbol: u for u in _convertir(db_session)}
    assert set(por_simbolo) == {"BTCUSDT", "ETHUSDT"}
    assert por_simbolo["ETHUSDT"].nivel == NivelIA.NORMAL and por_simbolo["ETHUSDT"].puntaje_ia is not None
    # El excluido por calidad queda sin propuesta: hoy es indistinguible de un omitido.
    assert por_simbolo["BTCUSDT"].nivel == NivelIA.NO_INCLUIDO and por_simbolo["BTCUSDT"].puntaje_ia is None
    assert all(u.es_candidato and u.recomendacion == "invertir" for u in por_simbolo.values())

    c = contar_muestra(list(por_simbolo.values()))
    assert (c.ciclos, c.ciclos_pareados, c.incluidos, c.no_incluidos) == (1, 1, 1, 1)
    assert not c.suficiente


def test_el_repositorio_no_escribe(monkeypatch, db_session):
    """Leer no deja filas nuevas ni cambia conteos."""
    from sqlalchemy import func, select

    from app.db.models.ai_calls import LlamadaIA
    from app.db.models.analysis import CicloAnalisis, PrefiltroResultado, Propuesta

    _seed_symbol(db_session, "ETHUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["ETHUSDT"]))
    monkeypatch.setattr("app.ai.agents._runner.call_agent", _make_fake_call_agent({"ETHUSDT": "suficiente"}))
    monkeypatch.setattr(_DQ_PATCH_TARGET, _perfect_data_quality)
    run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)

    def conteos():
        return [
            db_session.scalar(select(func.count()).select_from(m))
            for m in (CicloAnalisis, PrefiltroResultado, Propuesta, LlamadaIA)
        ]

    antes = conteos()
    _convertir(db_session)
    assert conteos() == antes
