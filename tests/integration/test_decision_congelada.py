"""La decisión congelada (sección 14 del master prompt) la impone la base de datos:
`propuestas` y `factores` rechazan modificar lo que describe la decisión."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.db.models.analysis import Factor, Propuesta
from app.domain.enums import EstadoCiclo
from app.repositories.analysis import create_ciclo, get_factores_de_propuesta, save_propuesta
from tests.integration.test_analysis_repository import _propuesta_row


def _guardar(db_session):
    ciclo_id = create_ciclo(db_session, started_at=datetime.now(timezone.utc), status=EstadoCiclo.CONSOLIDATE)
    propuesta_id = save_propuesta(db_session, _propuesta_row(ciclo_id))
    return ciclo_id, propuesta_id


@pytest.mark.parametrize(
    "asignacion",
    [
        "decision_snapshot = '{\"otro\": 1}'::jsonb",
        "puntaje_ponderado = 99",
        "puntaje_base = 99",
        "tesis = 'reescrita despues'",
        "invalidacion = 'reescrita despues'",
        "symbol = 'ETHUSDT'",
        "supera_umbral_entrada = NOT supera_umbral_entrada",
        "fuentes = '[]'::jsonb",
    ],
)
def test_la_base_rechaza_modificar_la_decision_de_una_propuesta(db_session, asignacion):
    _, propuesta_id = _guardar(db_session)
    with pytest.raises(DBAPIError, match="decision congelada"):
        with db_session.begin_nested():
            db_session.execute(text(f"UPDATE propuestas SET {asignacion} WHERE id = :i"), {"i": propuesta_id})


def test_una_propuesta_sigue_igual_tras_el_intento_rechazado(db_session):
    _, propuesta_id = _guardar(db_session)
    antes = db_session.execute(select(Propuesta.decision_snapshot, Propuesta.puntaje_ponderado)).first()
    with pytest.raises(DBAPIError):
        with db_session.begin_nested():
            db_session.execute(text("UPDATE propuestas SET puntaje_ponderado = 1 WHERE id = :i"), {"i": propuesta_id})
    despues = db_session.execute(select(Propuesta.decision_snapshot, Propuesta.puntaje_ponderado)).first()
    assert antes == despues


def test_actualizar_con_el_mismo_valor_o_columnas_ajenas_a_la_decision_si_se_permite(db_session):
    _, propuesta_id = _guardar(db_session)
    # created_at no describe la decisión (los tests de orden lo ajustan)
    db_session.execute(
        text("UPDATE propuestas SET created_at = :t WHERE id = :i"),
        {"t": datetime.now(timezone.utc) - timedelta(days=1), "i": propuesta_id},
    )
    # reescribir el mismo snapshot (sin cambio real) tampoco es una modificación
    db_session.execute(text("UPDATE propuestas SET decision_snapshot = decision_snapshot WHERE id = :i"), {"i": propuesta_id})


@pytest.mark.parametrize("asignacion", ["puntaje = 1", "peso = 0.99", "factor = 'otro'", "confianza = 'baja'"])
def test_la_base_rechaza_modificar_un_factor(db_session, asignacion):
    _, propuesta_id = _guardar(db_session)
    factores = get_factores_de_propuesta(db_session, propuesta_id)
    assert factores
    with pytest.raises(DBAPIError, match="decision congelada"):
        with db_session.begin_nested():
            db_session.execute(text(f"UPDATE factores SET {asignacion} WHERE id = :i"), {"i": factores[0].id})


def test_los_factores_siguen_igual_tras_el_intento_rechazado(db_session):
    _, propuesta_id = _guardar(db_session)
    antes = sorted((f.factor, f.puntaje, f.peso) for f in get_factores_de_propuesta(db_session, propuesta_id))
    with pytest.raises(DBAPIError):
        with db_session.begin_nested():
            db_session.execute(text("UPDATE factores SET puntaje = 1"))
    despues = sorted((f.factor, f.puntaje, f.peso) for f in db_session.scalars(select(Factor)) if f.propuesta_id == propuesta_id)
    assert antes == despues
