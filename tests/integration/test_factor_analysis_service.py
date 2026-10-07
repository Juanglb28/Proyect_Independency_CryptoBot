"""`analyze_factors_for_period`: une operaciones cerradas con los factores
guardados de su propuesta (sección 8.8)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select, update

from app.db.models.analysis import Factor
from app.repositories.analysis import list_factores_de_propuestas
from app.results.factor_analysis_service import analyze_factors_for_period
from app.results.performance_service import load_trades
from tests.integration.test_results_pipeline import seis_operaciones  # noqa: F401

D = Decimal


def test_sin_operaciones_devuelve_los_cuatro_factores_vacios(db_session):
    r = analyze_factors_for_period(db_session)
    assert r.n_operaciones == 0 and len(r.factores) == 4
    assert all(a.n_operaciones == 0 and a.contribucion_usd is None and a.peso is None for a in r.factores)


def test_list_factores_de_propuestas_vacio_y_con_datos(db_session, seis_operaciones):  # noqa: F811
    assert list_factores_de_propuestas(db_session, []) == []
    ids = [t.proposal_id for t in load_trades(db_session, period_start=None, period_end=None)]
    assert len(list_factores_de_propuestas(db_session, ids)) == 6 * 4


def test_factores_constantes_no_inventan_contribucion(db_session, seis_operaciones):  # noqa: F811
    r = analyze_factors_for_period(db_session)
    assert r.n_operaciones == r.n_con_propuesta_y_factores == 6
    for a in r.factores:
        assert a.n_operaciones == 6 and a.peso is not None
        assert a.contribucion_usd is None and a.discrimina is None
        assert any("mismo puntaje" in n for n in a.notas)


def test_puntaje_de_factor_alineado_con_el_resultado_da_contribucion_positiva(db_session, seis_operaciones):  # noqa: F811
    trades = load_trades(db_session, period_start=None, period_end=None)
    # el puntaje de "tendencia_momentum" sigue al score global (ganan los altos)
    for t in trades:
        db_session.execute(
            update(Factor)
            .where(Factor.propuesta_id == t.proposal_id, Factor.factor == "tendencia_momentum")
            .values(puntaje=t.initial_opportunity_score)
        )
    db_session.flush()
    r = analyze_factors_for_period(db_session)
    tm = next(a for a in r.factores if a.factor == "tendencia_momentum")
    assert tm.contribucion_usd == D("20.3796") and tm.tercil_alto.n == 2 and tm.tercil_bajo.n == 2
    assert tm.estado.value == "PRELIMINAR"
    # los demás siguen constantes
    assert all(a.contribucion_usd is None for a in r.factores if a.factor != "tendencia_momentum")


def test_el_analisis_no_escribe_nada(db_session, seis_operaciones):  # noqa: F811
    antes = [(f.id, f.puntaje) for f in db_session.scalars(select(Factor))]
    analyze_factors_for_period(db_session)
    assert [(f.id, f.puntaje) for f in db_session.scalars(select(Factor))] == antes
