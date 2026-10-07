"""`prefiltro_resultados`: persistencia, garantías de la base, escritura desde
el ciclo diario, lectura en los servicios y presentación en la interfaz."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from streamlit.testing.v1 import AppTest

from app.config import Settings
from app.data.prefilter import PREFILTER_FORMULA_VERSION, PrefilterScore
from app.data.prefilter_pipeline import PrefilterRunResult
from app.domain.enums import EstadoCiclo, EstadoPrefiltro
from app.orchestration.daily_cycle import guardar_resultado_prefiltro, run_daily_cycle
from app.repositories.analysis import (
    PrefiltroRow,
    create_ciclo,
    finalizar_ciclo,
    get_ciclo,
    list_prefiltro_de_ciclo,
    save_prefiltro_resultados,
)
from app.services import views
from app.services.decisions import NOTA_EMBUDO, NOTA_EMBUDO_REGISTRADO, get_ciclo_detalle

_T = datetime(2026, 10, 6, tzinfo=timezone.utc)
D = Decimal


def _score(symbol: str, final: str) -> PrefilterScore:
    f = D(final)
    return PrefilterScore(symbol, _T, D("5"), D("1.5"), D("-3"), f, f, f, f)


def _ciclo(db_session) -> object:
    return create_ciclo(db_session, started_at=_T, status=EstadoCiclo.PREFILTER)


def _fila(symbol="BTCUSDT", estado=EstadoPrefiltro.CANDIDATO, ranking=1, con_datos=True) -> PrefiltroRow:
    v = D("60") if con_datos else None
    return PrefiltroRow(
        symbol=symbol, estado=estado, ranking=ranking if con_datos else None,
        calculated_at=_T if con_datos else None,
        tendencia_pct=D("5") if con_datos else None, volumen_relativo=D("1.5") if con_datos else None,
        distancia_max_90d_pct=D("-3") if con_datos else None,
        score_tendencia=v, score_volumen=v, score_distancia=v, score_final=v,
    )


# --- repositorio y garantías de la base ---------------------------------------


def test_guarda_y_lee_ordenado_por_ranking_con_sin_datos_al_final(db_session):
    ciclo = _ciclo(db_session)
    save_prefiltro_resultados(
        db_session, ciclo,
        [
            _fila("ZZZ", EstadoPrefiltro.SIN_DATOS, con_datos=False),
            _fila("ETHUSDT", EstadoPrefiltro.DESCARTADO_POR_PUNTAJE, ranking=2),
            _fila("BTCUSDT", EstadoPrefiltro.CANDIDATO, ranking=1),
        ],
        formula_version="1",
    )
    filas = list_prefiltro_de_ciclo(db_session, ciclo)
    assert [(f.symbol, f.estado, f.ranking) for f in filas] == [
        ("BTCUSDT", EstadoPrefiltro.CANDIDATO, 1),
        ("ETHUSDT", EstadoPrefiltro.DESCARTADO_POR_PUNTAJE, 2),
        ("ZZZ", EstadoPrefiltro.SIN_DATOS, None),
    ]
    assert filas[0].score_final == D("60") and filas[0].formula_version == "1"
    assert filas[2].score_final is None and filas[2].tendencia_pct is None  # nunca un 0 inventado


def test_un_ciclo_sin_filas_devuelve_lista_vacia(db_session):
    assert list_prefiltro_de_ciclo(db_session, _ciclo(db_session)) == []


def test_no_permite_el_mismo_simbolo_dos_veces_en_un_ciclo(db_session):
    ciclo = _ciclo(db_session)
    save_prefiltro_resultados(db_session, ciclo, [_fila()], formula_version="1")
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            save_prefiltro_resultados(db_session, ciclo, [_fila()], formula_version="1")


def test_la_base_rechaza_sin_datos_con_puntaje_y_puntaje_sin_ranking(db_session):
    ciclo = _ciclo(db_session)
    incoherente = _fila("AAA", EstadoPrefiltro.SIN_DATOS, ranking=1)  # SIN_DATOS pero con puntaje
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            save_prefiltro_resultados(db_session, ciclo, [incoherente], formula_version="1")
    sin_ranking = _fila("BBB", EstadoPrefiltro.CANDIDATO, ranking=None)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            save_prefiltro_resultados(db_session, ciclo, [sin_ranking], formula_version="1")


def test_la_base_rechaza_modificar_o_borrar_un_resultado(db_session):
    ciclo = _ciclo(db_session)
    save_prefiltro_resultados(db_session, ciclo, [_fila()], formula_version="1")
    with pytest.raises(DBAPIError, match="solo-agregar"):
        with db_session.begin_nested():
            db_session.execute(text("UPDATE prefiltro_resultados SET score_final = 99"))
    with pytest.raises(DBAPIError, match="solo-agregar"):
        with db_session.begin_nested():
            db_session.execute(text("DELETE FROM prefiltro_resultados"))


# --- escritura desde el ciclo diario -------------------------------------------


def _settings() -> Settings:
    return Settings(database_url="postgresql+psycopg://x:x@localhost/x", anthropic_api_key="sk-test")


def test_guardar_resultado_prefiltro_traduce_scores_candidatos_y_sin_datos(db_session):
    ciclo = _ciclo(db_session)
    a, b = _score("AAA", "80"), _score("BBB", "30")
    guardar_resultado_prefiltro(
        db_session, ciclo,
        PrefilterRunResult(scores=(a, b), candidates=(a,), symbols_sin_datos=("CCC",)),
    )
    filas = {f.symbol: f for f in list_prefiltro_de_ciclo(db_session, ciclo)}
    assert filas["AAA"].estado == EstadoPrefiltro.CANDIDATO and filas["AAA"].ranking == 1
    assert filas["BBB"].estado == EstadoPrefiltro.DESCARTADO_POR_PUNTAJE and filas["BBB"].ranking == 2
    assert filas["CCC"].estado == EstadoPrefiltro.SIN_DATOS and filas["CCC"].score_final is None
    assert {f.formula_version for f in filas.values()} == {PREFILTER_FORMULA_VERSION}
    assert filas["AAA"].tendencia_pct == D("5") and filas["AAA"].calculated_at == _T


def test_el_ciclo_guarda_el_prefiltro_aunque_termine_sin_candidatos(db_session, monkeypatch):
    bajo = _score("AAA", "30")
    monkeypatch.setattr(
        "app.orchestration.daily_cycle.run_prefilter",
        lambda session: PrefilterRunResult(scores=(bajo,), candidates=(), symbols_sin_datos=("CCC",)),
    )
    resultado = run_daily_cycle(db_session, _settings(), now=_T)  # sin candidatos: cero llamadas a la IA
    assert resultado.candidatos_evaluados == ()
    ciclo = get_ciclo(db_session, resultado.ciclo_id)
    assert ciclo.finished_at is not None and ciclo.error_message is None
    assert [(f.symbol, f.estado) for f in list_prefiltro_de_ciclo(db_session, resultado.ciclo_id)] == [
        ("AAA", EstadoPrefiltro.DESCARTADO_POR_PUNTAJE),
        ("CCC", EstadoPrefiltro.SIN_DATOS),
    ]


# --- servicio de lectura -------------------------------------------------------


def test_el_detalle_incluye_el_prefiltro_y_la_nota_de_registrado(db_session):
    ciclo = _ciclo(db_session)
    save_prefiltro_resultados(db_session, ciclo, [_fila()], formula_version="1")
    d = get_ciclo_detalle(db_session, ciclo)
    assert [(p.symbol, p.estado, p.formula_version) for p in d.prefiltro] == [("BTCUSDT", EstadoPrefiltro.CANDIDATO, "1")]
    assert d.nota_embudo == NOTA_EMBUDO_REGISTRADO


def test_un_ciclo_anterior_sin_filas_conserva_la_nota_de_limitacion(db_session):
    ciclo = _ciclo(db_session)
    d = get_ciclo_detalle(db_session, ciclo)
    assert d.prefiltro == () and d.nota_embudo == NOTA_EMBUDO


# --- interfaz ------------------------------------------------------------------


def _pagina() -> None:
    import importlib

    importlib.import_module("app.ui.paginas.analisis").render()


def _abrir_analisis(db_session, monkeypatch) -> AppTest:
    @contextmanager
    def _sesion(factory=None):
        yield db_session

    monkeypatch.setattr(views, "read_session", _sesion)
    return AppTest.from_function(_pagina, default_timeout=60).run()


def test_la_pagina_analisis_muestra_el_embudo_del_prefiltro(db_session, monkeypatch):
    ciclo = _ciclo(db_session)
    finalizar_ciclo(db_session, ciclo, status=EstadoCiclo.PREFILTER, finished_at=_T, cost_usd=D("0"), error_message=None)
    save_prefiltro_resultados(
        db_session, ciclo,
        [_fila("BTCUSDT", EstadoPrefiltro.CANDIDATO, 1), _fila("ETHUSDT", EstadoPrefiltro.DESCARTADO_POR_PUNTAJE, 2),
         _fila("ZZZ", EstadoPrefiltro.SIN_DATOS, con_datos=False)],
        formula_version="1",
    )
    at = _abrir_analisis(db_session, monkeypatch)
    assert not at.exception, [e.value for e in at.exception]
    textos = "\n".join(str(e.value) for e in list(at.markdown) + list(at.caption))
    assert "3 activos del universo → 2 con puntaje → 1 candidatos" in textos
    tabla = at.dataframe[0].value
    assert list(tabla["Activo"]) == ["BTCUSDT", "ETHUSDT", "ZZZ"]
    assert tabla["Puntaje"].tolist()[2] == "n/d"  # sin datos: n/d, no 0
