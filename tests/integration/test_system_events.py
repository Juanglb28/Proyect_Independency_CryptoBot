"""Tests de `eventos_sistema` contra Postgres real (Paso 8, decisión del
2026-10-04): restricciones de la tabla, trigger de solo-agregar y
transiciones protegidas del repositorio."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.models.system_events import EventoSistema
from app.domain.enums import TipoEventoSistema
from app.repositories.system_events import (
    EventoSistemaRow,
    append_evento_sistema,
    get_ultimo_evento_sistema,
    list_eventos_sistema,
)

_AHORA = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _pausa(**kw) -> EventoSistemaRow:
    base = dict(
        event_type=TipoEventoSistema.PAUSA_DRAWDOWN,
        occurred_at=_AHORA,
        equity_usd=Decimal("850"),
        peak_equity_usd=Decimal("1000"),
    )
    base.update(kw)
    return EventoSistemaRow(**base)


def _reanudacion(**kw) -> EventoSistemaRow:
    base = dict(
        event_type=TipoEventoSistema.REANUDACION,
        occurred_at=_AHORA,
        equity_usd=Decimal("850"),
        peak_equity_usd=Decimal("850"),
        previous_peak_equity_usd=Decimal("1000"),
    )
    base.update(kw)
    return EventoSistemaRow(**base)


# --- repositorio: transiciones ---


def test_sin_eventos_no_hay_ultimo(db_session):
    assert get_ultimo_evento_sistema(db_session) is None


def test_pausa_y_reanudacion_se_guardan_con_valores_exactos(db_session):
    assert append_evento_sistema(db_session, _pausa(detail="drawdown -15%")) is True
    assert append_evento_sistema(db_session, _reanudacion()) is True

    eventos = list_eventos_sistema(db_session)
    assert [e.event_type for e in eventos] == [TipoEventoSistema.PAUSA_DRAWDOWN, TipoEventoSistema.REANUDACION]
    assert eventos[0].equity_usd == Decimal("850") and eventos[0].peak_equity_usd == Decimal("1000")
    assert eventos[0].previous_peak_equity_usd is None
    assert eventos[1].peak_equity_usd == Decimal("850")  # pico nuevo = equity al reanudar
    assert eventos[1].previous_peak_equity_usd == Decimal("1000")  # el anterior queda como registro
    assert eventos[0].occurred_at.tzinfo is not None
    assert get_ultimo_evento_sistema(db_session).event_type == TipoEventoSistema.REANUDACION


def test_pausa_repetida_no_duplica(db_session):
    assert append_evento_sistema(db_session, _pausa()) is True
    assert append_evento_sistema(db_session, _pausa(equity_usd=Decimal("800"))) is False
    assert len(list_eventos_sistema(db_session)) == 1


def test_reanudar_sin_pausa_previa_no_hace_nada(db_session):
    assert append_evento_sistema(db_session, _reanudacion()) is False
    assert list_eventos_sistema(db_session) == []


def test_reanudacion_repetida_no_duplica(db_session):
    append_evento_sistema(db_session, _pausa())
    assert append_evento_sistema(db_session, _reanudacion()) is True
    assert append_evento_sistema(db_session, _reanudacion()) is False
    assert len(list_eventos_sistema(db_session)) == 2


def test_puede_pausarse_de_nuevo_despues_de_reanudar(db_session):
    append_evento_sistema(db_session, _pausa())
    append_evento_sistema(db_session, _reanudacion())
    assert append_evento_sistema(db_session, _pausa(equity_usd=Decimal("722.5"), peak_equity_usd=Decimal("850"))) is True
    assert get_ultimo_evento_sistema(db_session).event_type == TipoEventoSistema.PAUSA_DRAWDOWN


def test_el_ultimo_se_decide_por_id_y_no_por_la_hora_registrada(db_session):
    append_evento_sistema(db_session, _pausa(occurred_at=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)))
    # La reanudación trae una hora ANTERIOR (reloj desajustado): sigue siendo el último.
    append_evento_sistema(db_session, _reanudacion(occurred_at=datetime(2026, 10, 4, 11, 0, tzinfo=timezone.utc)))
    assert get_ultimo_evento_sistema(db_session).event_type == TipoEventoSistema.REANUDACION


# --- restricciones de la tabla ---


def _debe_violar(db_session, **valores):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(EventoSistema(**valores))
            db_session.flush()


def _valores(**kw):
    base = dict(
        event_type=TipoEventoSistema.PAUSA_DRAWDOWN,
        occurred_at=_AHORA,
        equity_usd=Decimal("850"),
        peak_equity_usd=Decimal("1000"),
    )
    base.update(kw)
    return base


def test_reanudacion_sin_pico_previo_es_invalida(db_session):
    _debe_violar(db_session, **_valores(event_type=TipoEventoSistema.REANUDACION, peak_equity_usd=Decimal("850")))


def test_reanudacion_con_pico_distinto_del_equity_es_invalida(db_session):
    _debe_violar(
        db_session,
        **_valores(
            event_type=TipoEventoSistema.REANUDACION,
            peak_equity_usd=Decimal("1000"),  # debería ser 850: el pico se reinicia
            previous_peak_equity_usd=Decimal("1000"),
        ),
    )


def test_pausa_con_pico_previo_es_invalida(db_session):
    _debe_violar(db_session, **_valores(previous_peak_equity_usd=Decimal("1000")))


def test_valores_no_positivos_son_invalidos(db_session):
    _debe_violar(db_session, **_valores(equity_usd=Decimal("-1")))
    _debe_violar(db_session, **_valores(peak_equity_usd=Decimal("0")))


# --- solo-agregar (trigger) ---


def test_no_se_puede_modificar_un_evento(db_session):
    append_evento_sistema(db_session, _pausa())
    with pytest.raises(DBAPIError, match="solo-agregar"):
        with db_session.begin_nested():
            db_session.execute(text("UPDATE eventos_sistema SET equity_usd = 1"))


def test_no_se_puede_borrar_un_evento(db_session):
    append_evento_sistema(db_session, _pausa())
    with pytest.raises(DBAPIError, match="solo-agregar"):
        with db_session.begin_nested():
            db_session.execute(text("DELETE FROM eventos_sistema"))
    assert len(list_eventos_sistema(db_session)) == 1
