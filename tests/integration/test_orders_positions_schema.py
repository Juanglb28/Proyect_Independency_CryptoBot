"""Tests de las restricciones de `ordenes` y `posiciones` contra Postgres
real (Paso 8, diseño aprobado el 2026-10-04).

Estas tablas todavía no tienen repositorio ni servicio: aquí se prueba lo
que debe garantizar la BASE DE DATOS por sí sola (P5), insertando filas
directamente con el ORM. Cada violación se espera dentro de un SAVEPOINT
(`begin_nested`) para no abortar la transacción del test.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError, StatementError

from app.db.models.orders import Orden
from app.db.models.positions import Posicion
from app.domain.enums import EstadoOrden, EstadoPosicion, IntencionOrden, LadoOrden
from app.repositories.analysis import create_ciclo, save_propuesta
from app.domain.enums import EstadoCiclo
from tests.integration.test_analysis_repository import _propuesta_row

_AHORA = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _propuesta(db_session, symbol: str = "BTCUSDT") -> uuid.UUID:
    ciclo_id = create_ciclo(db_session, started_at=_AHORA, status=EstadoCiclo.PREFILTER)
    return save_propuesta(db_session, _propuesta_row(ciclo_id, symbol))


def _posicion(proposal_id, symbol: str = "BTCUSDT", **overrides) -> Posicion:
    defaults = dict(
        proposal_id=proposal_id,
        symbol=symbol,
        status=EstadoPosicion.OPEN,
        entry_price=Decimal("100"),
        initial_quantity=Decimal("1"),
        remaining_quantity=Decimal("1"),
        initial_stop_price=Decimal("92"),
        initial_target_price=Decimal("112"),
        opened_at=_AHORA,
    )
    defaults.update(overrides)
    return Posicion(**defaults)


def _orden(proposal_id, **overrides) -> Orden:
    defaults = dict(
        client_order_id=f"test-{uuid.uuid4()}",
        proposal_id=proposal_id,
        symbol="BTCUSDT",
        side=LadoOrden.BUY,
        intencion=IntencionOrden.ENTRY,
        estado=EstadoOrden.PENDING_APPROVAL,
        requested_usd=Decimal("100"),
    )
    defaults.update(overrides)
    return Orden(**defaults)


def _debe_violar(db_session, *objetos) -> None:
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            for o in objetos:
                db_session.add(o)
            db_session.flush()


# --- posiciones ---


def test_posicion_abierta_se_persiste_con_valores_exactos_y_defaults(db_session):
    pos = _posicion(_propuesta(db_session))
    db_session.add(pos)
    db_session.flush()
    db_session.refresh(pos)

    assert pos.id is not None
    assert pos.entry_price == Decimal("100")
    assert pos.take_profit_hit is False
    assert pos.trailing_active is False
    assert pos.trailing_peak_price is None
    assert pos.opened_at.tzinfo is not None  # TIMESTAMPTZ
    assert pos.created_at is not None


def test_un_simbolo_no_puede_tener_dos_posiciones_abiertas(db_session):
    p1, p2 = _propuesta(db_session), _propuesta(db_session)
    db_session.add(_posicion(p1))
    db_session.flush()
    _debe_violar(db_session, _posicion(p2))


def test_un_simbolo_puede_reabrirse_despues_de_cerrar_la_anterior(db_session):
    p1, p2 = _propuesta(db_session), _propuesta(db_session)
    db_session.add(
        _posicion(p1, status=EstadoPosicion.CLOSED, remaining_quantity=Decimal(0), closed_at=_AHORA)
    )
    db_session.add(_posicion(p2))
    db_session.flush()  # no debe lanzar: el índice único solo cubre las OPEN


def test_dos_simbolos_distintos_pueden_estar_abiertos_a_la_vez(db_session):
    db_session.add(_posicion(_propuesta(db_session, "BTCUSDT"), symbol="BTCUSDT"))
    db_session.add(_posicion(_propuesta(db_session, "ETHUSDT"), symbol="ETHUSDT"))
    db_session.flush()


def test_una_propuesta_no_puede_abrir_dos_posiciones(db_session):
    proposal_id = _propuesta(db_session)
    db_session.add(_posicion(proposal_id, status=EstadoPosicion.CLOSED, remaining_quantity=Decimal(0), closed_at=_AHORA))
    db_session.flush()
    _debe_violar(db_session, _posicion(proposal_id))


def test_posicion_abierta_sin_cantidad_restante_es_invalida(db_session):
    _debe_violar(db_session, _posicion(_propuesta(db_session), remaining_quantity=Decimal(0)))


def test_posicion_cerrada_con_cantidad_restante_es_invalida(db_session):
    _debe_violar(
        db_session,
        _posicion(_propuesta(db_session), status=EstadoPosicion.CLOSED, closed_at=_AHORA),
    )


def test_posicion_cerrada_sin_closed_at_es_invalida(db_session):
    _debe_violar(
        db_session,
        _posicion(_propuesta(db_session), status=EstadoPosicion.CLOSED, remaining_quantity=Decimal(0)),
    )


def test_cantidad_restante_no_puede_superar_la_inicial(db_session):
    _debe_violar(db_session, _posicion(_propuesta(db_session), remaining_quantity=Decimal("1.000000000001")))


def test_cierre_parcial_sigue_siendo_una_posicion_abierta_valida(db_session):
    pos = _posicion(
        _propuesta(db_session),
        remaining_quantity=Decimal("0.5"),
        take_profit_hit=True,
        trailing_active=True,
        trailing_peak_price=Decimal("112.5"),
    )
    db_session.add(pos)
    db_session.flush()


def test_trailing_activo_exige_precio_maximo_de_referencia(db_session):
    _debe_violar(db_session, _posicion(_propuesta(db_session), trailing_active=True))


@pytest.mark.parametrize("campo", ["entry_price", "initial_stop_price", "initial_target_price", "initial_quantity"])
def test_precios_y_cantidad_inicial_deben_ser_positivos(db_session, campo):
    overrides = {campo: Decimal(0)}
    if campo == "initial_quantity":
        overrides["remaining_quantity"] = Decimal(0)  # evita otro check; el que falla es el de positividad
    _debe_violar(db_session, _posicion(_propuesta(db_session), **overrides))


# --- ordenes ---


def test_orden_pendiente_se_persiste_sin_posicion_ni_cantidad(db_session):
    orden = _orden(_propuesta(db_session), expires_at=_AHORA)
    db_session.add(orden)
    db_session.flush()
    db_session.refresh(orden)

    assert orden.position_id is None
    assert orden.quantity is None
    assert orden.requested_usd == Decimal("100")
    assert orden.estado == EstadoOrden.PENDING_APPROVAL
    assert orden.created_at.tzinfo is not None


def test_client_order_id_es_unico(db_session):
    p1, p2 = _propuesta(db_session), _propuesta(db_session)
    db_session.add(_orden(p1, client_order_id="entrada-123"))
    db_session.flush()
    _debe_violar(db_session, _orden(p2, client_order_id="entrada-123"))


def test_una_propuesta_no_puede_tener_dos_ordenes_de_entrada_aunque_cambie_el_client_order_id(db_session):
    proposal_id = _propuesta(db_session)
    db_session.add(_orden(proposal_id, client_order_id="a"))
    db_session.flush()
    _debe_violar(db_session, _orden(proposal_id, client_order_id="b"))


def test_una_propuesta_puede_tener_ordenes_de_entrada_y_de_salida(db_session):
    proposal_id = _propuesta(db_session)
    pos = _posicion(proposal_id)
    db_session.add(pos)
    db_session.flush()
    db_session.add(
        _orden(
            proposal_id,
            position_id=pos.id,
            estado=EstadoOrden.FILLED,
            quantity=Decimal("1"),
            execution_price=Decimal("100"),
            fee=Decimal("0.1"),
            filled_at=_AHORA,
        )
    )
    db_session.add(
        _orden(
            proposal_id,
            position_id=pos.id,
            side=LadoOrden.SELL,
            intencion=IntencionOrden.STOP_LOSS,
            estado=EstadoOrden.FILLED,
            requested_usd=None,
            quantity=Decimal("1"),
            reference_price=Decimal("91"),
            execution_price=Decimal("91"),
            fee=Decimal("0.091"),
            filled_at=_AHORA,
        )
    )
    db_session.flush()


@pytest.mark.parametrize("campo_faltante", ["quantity", "execution_price", "fee", "filled_at"])
def test_orden_filled_incompleta_es_invalida(db_session, campo_faltante):
    datos = dict(
        estado=EstadoOrden.FILLED,
        quantity=Decimal("1"),
        execution_price=Decimal("100"),
        fee=Decimal("0.1"),
        filled_at=_AHORA,
    )
    datos[campo_faltante] = None
    _debe_violar(db_session, _orden(_propuesta(db_session), **datos))


@pytest.mark.parametrize(
    "campo,valor",
    [
        ("quantity", Decimal(0)),
        ("execution_price", Decimal(0)),
        ("reference_price", Decimal("-1")),
        ("fee", Decimal("-0.01")),
        ("requested_usd", Decimal(0)),
    ],
)
def test_valores_no_positivos_en_una_orden_son_invalidos(db_session, campo, valor):
    _debe_violar(db_session, _orden(_propuesta(db_session), **{campo: valor}))


def test_orden_con_posicion_inexistente_es_invalida(db_session):
    _debe_violar(db_session, _orden(_propuesta(db_session), position_id=uuid.uuid4()))


def test_orden_con_propuesta_inexistente_es_invalida(db_session):
    _debe_violar(db_session, _orden(uuid.uuid4()))


def test_estado_o_intencion_fuera_del_enum_se_rechaza_antes_de_llegar_a_la_base(db_session):
    # validate_strings=True: un valor inventado falla en Python, nunca se persiste.
    with pytest.raises(StatementError, match="not among the defined enum values"):
        with db_session.begin_nested():
            db_session.add(_orden(_propuesta(db_session), estado="INVENTADO"))
            db_session.flush()


def test_el_dinero_se_guarda_sin_perdida_de_precision(db_session):
    # Nunca float: 0.1 USD de comisión y 12 decimales de cantidad vuelven idénticos.
    orden = _orden(
        _propuesta(db_session),
        estado=EstadoOrden.FILLED,
        quantity=Decimal("0.001234567891"),
        execution_price=Decimal("81000.123456789012"),
        fee=Decimal("0.10000000"),
        filled_at=_AHORA,
    )
    db_session.add(orden)
    db_session.flush()
    db_session.expire(orden)
    assert orden.quantity == Decimal("0.001234567891")
    assert orden.execution_price == Decimal("81000.123456789012")
    assert orden.fee == Decimal("0.10000000")
