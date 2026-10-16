"""Persistencia de `kronos_forecasts` (protocolo K1): idempotencia, lectura y solo-agregar."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.models.kronos_forecasts import KronosForecast
from app.repositories.kronos_forecasts import ForecastRow, append_forecast, claves_existentes, listar_forecasts

D = Decimal
T0 = datetime(2024, 7, 1, tzinfo=timezone.utc)
T1 = datetime(2024, 7, 4, tzinfo=timezone.utc)
CLAVE = dict(weights_revision="a" * 40, lookback=400, horizon=24, n_trajectories=3, seed=20261015)


def _fila(symbol="BTCUSDT", as_of=T0, **kw) -> ForecastRow:
    base = dict(
        symbol=symbol, as_of=as_of, model_id="NeoQuasar/Kronos-small", weights_revision="a" * 40,
        tokenizer_id="NeoQuasar/Kronos-Tokenizer-base", tokenizer_revision="b" * 40, code_commit="c" * 40,
        lookback=400, horizon=24, n_trajectories=3, temperature=D("1.0"), top_p=D("0.9"), seed=20261015,
        uses_volume=False, device="cpu", environment='{"torch": "2.x"}', protocol_sha256="d" * 64,
        last_close=D("100"), final_closes=(D("101"), D("99"), D("102")), expected_return=D("0.006667"),
        prob_up=D("0.666667"), dispersion=D("0.015275"),
    )
    base.update(kw)
    return ForecastRow(**base)


def test_agrega_y_lee_con_los_mismos_valores(db_session):
    assert append_forecast(db_session, _fila()) is True
    (leida,) = listar_forecasts(db_session, **CLAVE)
    assert leida == _fila()
    assert isinstance(leida.final_closes[0], Decimal)  # nunca float


def test_repetir_el_pronostico_no_duplica_ni_modifica(db_session):
    assert append_forecast(db_session, _fila()) is True
    assert append_forecast(db_session, _fila(expected_return=D("0.5"))) is False  # misma clave: se ignora
    (leida,) = listar_forecasts(db_session, **CLAVE)
    assert leida.expected_return == D("0.006667")


def test_distinta_semilla_o_revision_son_pronosticos_distintos(db_session):
    assert append_forecast(db_session, _fila()) and append_forecast(db_session, _fila(seed=1))
    assert append_forecast(db_session, _fila(weights_revision="e" * 40))
    assert len(listar_forecasts(db_session, **CLAVE)) == 1


def test_claves_existentes_permite_reanudar(db_session):
    append_forecast(db_session, _fila("BTCUSDT", T0))
    append_forecast(db_session, _fila("ETHUSDT", T1))
    append_forecast(db_session, _fila("ETHUSDT", T0, seed=7))  # otra configuración: no cuenta
    assert claves_existentes(db_session, **CLAVE) == {("BTCUSDT", T0), ("ETHUSDT", T1)}


def test_el_listado_sale_ordenado_por_fecha_y_simbolo(db_session):
    for s, a in [("ETHUSDT", T1), ("BTCUSDT", T1), ("ETHUSDT", T0)]:
        append_forecast(db_session, _fila(s, a))
    assert [(f.as_of, f.symbol) for f in listar_forecasts(db_session, **CLAVE)] == [
        (T0, "ETHUSDT"), (T1, "BTCUSDT"), (T1, "ETHUSDT"),
    ]


def test_un_pronostico_generado_no_se_modifica_ni_se_borra(db_session):
    append_forecast(db_session, _fila())
    with pytest.raises(DBAPIError, match="kronos_forecasts"):
        with db_session.begin_nested():
            db_session.execute(text("UPDATE kronos_forecasts SET expected_return = 1"))
    with pytest.raises(DBAPIError, match="kronos_forecasts"):
        with db_session.begin_nested():
            db_session.execute(text("DELETE FROM kronos_forecasts"))
    assert len(listar_forecasts(db_session, **CLAVE)) == 1


@pytest.mark.parametrize(
    "cambio",
    [
        dict(final_closes=(D("101"),)),  # una cifra por trayectoria
        dict(prob_up=D("1.5")),
        dict(last_close=D("0")),
        dict(n_trajectories=0, final_closes=()),
    ],
)
def test_la_base_rechaza_filas_incoherentes(db_session, cambio):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            append_forecast(db_session, replace(_fila(), **cambio))


def test_no_hay_claves_foraneas_hacia_tablas_operativas(db_session):
    """P2: la tabla del experimento no se enlaza con nada operativo."""
    fks = db_session.execute(
        text(
            "SELECT count(*) FROM information_schema.table_constraints "
            "WHERE table_name = 'kronos_forecasts' AND constraint_type = 'FOREIGN KEY'"
        )
    ).scalar_one()
    assert fks == 0
