"""Tests de `equity_observaciones` contra Postgres real (Paso 8, decisión del
2026-10-05): restricciones de la tabla, trigger de solo-agregar y orden por
`id` del repositorio."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.models.equity import EquityObservacion
from app.domain.enums import OrigenObservacionEquity
from app.repositories.equity import (
    EquityObservacionRow,
    append_observacion,
    get_ultima_observacion,
    list_observaciones,
)

_AHORA = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)


def _row(**kw) -> EquityObservacionRow:
    base = dict(
        observed_at=_AHORA,
        origen=OrigenObservacionEquity.MONITOR,
        equity_usd=Decimal("999.9"),
        peak_equity_usd=Decimal("1000"),
        open_positions=1,
    )
    base.update(kw)
    return EquityObservacionRow(**base)


def test_sin_observaciones_no_hay_ultima(db_session):
    assert get_ultima_observacion(db_session) is None


def test_se_guarda_con_valores_exactos_y_zona_horaria(db_session):
    append_observacion(db_session, _row())
    ultima = get_ultima_observacion(db_session)
    assert ultima.equity_usd == Decimal("999.9")
    assert ultima.peak_equity_usd == Decimal("1000")
    assert ultima.open_positions == 1
    assert ultima.origen == OrigenObservacionEquity.MONITOR
    assert ultima.observed_at.tzinfo is not None


def test_la_ultima_se_decide_por_id_y_no_por_la_hora_registrada(db_session):
    append_observacion(db_session, _row(observed_at=datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)))
    append_observacion(
        db_session,
        _row(observed_at=datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc), peak_equity_usd=Decimal("1010")),
    )
    assert get_ultima_observacion(db_session).peak_equity_usd == Decimal("1010")
    assert [o.peak_equity_usd for o in list_observaciones(db_session)] == [Decimal("1000"), Decimal("1010")]


# --- restricciones de la tabla ---


def _debe_violar(db_session, **valores):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(EquityObservacion(**valores))
            db_session.flush()


def _valores(**kw):
    base = dict(
        observed_at=_AHORA,
        origen=OrigenObservacionEquity.MONITOR,
        equity_usd=Decimal("999.9"),
        peak_equity_usd=Decimal("1000"),
        open_positions=0,
    )
    base.update(kw)
    return base


def test_pico_menor_que_el_equity_es_invalido(db_session):
    _debe_violar(db_session, **_valores(equity_usd=Decimal("1001"), peak_equity_usd=Decimal("1000")))


def test_valores_negativos_o_cero_son_invalidos(db_session):
    _debe_violar(db_session, **_valores(equity_usd=Decimal("-1")))
    _debe_violar(db_session, **_valores(peak_equity_usd=Decimal("0"), equity_usd=Decimal("0")))
    _debe_violar(db_session, **_valores(open_positions=-1))


def test_reanudacion_con_pico_distinto_del_equity_es_invalida(db_session):
    _debe_violar(
        db_session,
        **_valores(origen=OrigenObservacionEquity.REANUDACION, equity_usd=Decimal("850"), peak_equity_usd=Decimal("1000")),
    )


def test_reanudacion_con_pico_igual_al_equity_es_valida(db_session):
    append_observacion(
        db_session,
        _row(origen=OrigenObservacionEquity.REANUDACION, equity_usd=Decimal("850"), peak_equity_usd=Decimal("850")),
    )
    assert get_ultima_observacion(db_session).peak_equity_usd == Decimal("850")


# --- solo-agregar (trigger) ---


def test_no_se_puede_modificar_una_observacion(db_session):
    append_observacion(db_session, _row())
    with pytest.raises(DBAPIError, match="solo-agregar"):
        with db_session.begin_nested():
            db_session.execute(text("UPDATE equity_observaciones SET equity_usd = 1"))


def test_no_se_puede_borrar_una_observacion(db_session):
    append_observacion(db_session, _row())
    with pytest.raises(DBAPIError, match="solo-agregar"):
        with db_session.begin_nested():
            db_session.execute(text("DELETE FROM equity_observaciones"))
    assert len(list_observaciones(db_session)) == 1
