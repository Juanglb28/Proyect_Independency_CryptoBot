"""Tests de los repositorios de `ordenes` y `posiciones` contra Postgres
real (Paso 8). Los repositorios solo persisten: aquí se prueba idempotencia
(P5), transiciones protegidas y consultas — no reglas de negocio."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.enums import EstadoCiclo, EstadoOrden, EstadoPosicion, IntencionOrden, LadoOrden
from app.repositories.analysis import create_ciclo, save_propuesta
from app.repositories.orders import (
    OrdenRow,
    count_entry_orders,
    create_orden,
    get_orden,
    get_orden_by_client_order_id,
    list_orders,
    list_orders_for_position,
    list_pending_orders_expired_at,
    resolve_pending_order,
)
from app.repositories.positions import (
    PosicionRow,
    count_open_posiciones,
    create_posicion,
    get_open_posicion_by_symbol,
    get_posicion,
    get_posicion_by_proposal,
    list_open_posiciones,
    update_open_position,
)
from tests.integration.test_analysis_repository import _propuesta_row

_AHORA = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
_INICIO_DIA = datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)
_FIN_DIA = _INICIO_DIA + timedelta(days=1)


def _propuesta(db_session, symbol: str = "BTCUSDT") -> uuid.UUID:
    ciclo_id = create_ciclo(db_session, started_at=_AHORA, status=EstadoCiclo.PREFILTER)
    return save_propuesta(db_session, _propuesta_row(ciclo_id, symbol))


def _entrada_pendiente(proposal_id, client_order_id: str = "ent-1", **kw) -> OrdenRow:
    base = dict(
        client_order_id=client_order_id,
        proposal_id=proposal_id,
        symbol="BTCUSDT",
        side=LadoOrden.BUY,
        intencion=IntencionOrden.ENTRY,
        estado=EstadoOrden.PENDING_APPROVAL,
        requested_usd=Decimal("100"),
        expires_at=_FIN_DIA,
    )
    base.update(kw)
    return OrdenRow(**base)


def _fila_posicion(proposal_id, symbol: str = "BTCUSDT") -> PosicionRow:
    return PosicionRow(
        proposal_id=proposal_id,
        symbol=symbol,
        entry_price=Decimal("100"),
        initial_quantity=Decimal("1"),
        initial_stop_price=Decimal("92"),
        initial_target_price=Decimal("112"),
        opened_at=_AHORA,
    )


# --- órdenes: idempotencia ---


def test_create_orden_persiste_y_devuelve_creada(db_session):
    pid = _propuesta(db_session)
    orden_id, creada = create_orden(db_session, _entrada_pendiente(pid))

    assert creada is True
    orden = get_orden(db_session, orden_id)
    assert orden.client_order_id == "ent-1"
    assert orden.estado == EstadoOrden.PENDING_APPROVAL
    assert orden.requested_usd == Decimal("100")
    assert orden.created_at is not None


def test_reintento_con_mismo_client_order_id_no_duplica_ni_modifica(db_session):
    pid = _propuesta(db_session)
    id1, creada1 = create_orden(db_session, _entrada_pendiente(pid, detail="original"))
    id2, creada2 = create_orden(db_session, _entrada_pendiente(pid, detail="reintento"))

    assert (creada1, creada2) == (True, False)
    assert id1 == id2
    assert len(list_orders(db_session, estados=list(EstadoOrden))) == 1
    db_session.refresh(get_orden(db_session, id1))
    assert get_orden(db_session, id1).detail == "original"


def test_segunda_entrada_para_la_misma_propuesta_con_otro_id_no_se_silencia(db_session):
    pid = _propuesta(db_session)
    create_orden(db_session, _entrada_pendiente(pid, "ent-1"))
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            create_orden(db_session, _entrada_pendiente(pid, "ent-2"))


def test_get_orden_by_client_order_id(db_session):
    pid = _propuesta(db_session)
    orden_id, _ = create_orden(db_session, _entrada_pendiente(pid))
    assert get_orden_by_client_order_id(db_session, "ent-1").id == orden_id
    assert get_orden_by_client_order_id(db_session, "no-existe") is None


# --- órdenes: transiciones ---


def test_aprobar_pendiente_la_llena_con_todos_los_datos(db_session):
    pid = _propuesta(db_session)
    orden_id, _ = create_orden(db_session, _entrada_pendiente(pid))
    pos_id, _ = create_posicion(db_session, _fila_posicion(pid))

    ok = resolve_pending_order(
        db_session,
        orden_id,
        nuevo_estado=EstadoOrden.FILLED,
        resolved_at=_AHORA,
        position_id=pos_id,
        quantity=Decimal("1"),
        execution_price=Decimal("100"),
        fee=Decimal("0.1"),
        filled_at=_AHORA,
    )

    assert ok is True
    orden = get_orden(db_session, orden_id)
    db_session.refresh(orden)
    assert orden.estado == EstadoOrden.FILLED
    assert orden.position_id == pos_id
    assert orden.execution_price == Decimal("100")
    assert orden.resolved_at == _AHORA


def test_segunda_resolucion_de_la_misma_orden_no_hace_nada(db_session):
    pid = _propuesta(db_session)
    orden_id, _ = create_orden(db_session, _entrada_pendiente(pid))

    assert resolve_pending_order(
        db_session, orden_id, nuevo_estado=EstadoOrden.REJECTED, resolved_at=_AHORA, detail="no"
    )
    assert not resolve_pending_order(
        db_session, orden_id, nuevo_estado=EstadoOrden.EXPIRED, resolved_at=_AHORA, detail="tarde"
    )
    orden = get_orden(db_session, orden_id)
    db_session.refresh(orden)
    assert orden.estado == EstadoOrden.REJECTED
    assert orden.detail == "no"


def test_resolver_orden_inexistente_devuelve_false(db_session):
    assert not resolve_pending_order(
        db_session, uuid.uuid4(), nuevo_estado=EstadoOrden.REJECTED, resolved_at=_AHORA
    )


def test_no_se_puede_resolver_hacia_pending(db_session):
    pid = _propuesta(db_session)
    orden_id, _ = create_orden(db_session, _entrada_pendiente(pid))
    with pytest.raises(ValueError):
        resolve_pending_order(
            db_session, orden_id, nuevo_estado=EstadoOrden.PENDING_APPROVAL, resolved_at=_AHORA
        )


def test_llenar_sin_datos_completos_lo_rechaza_la_base_de_datos(db_session):
    pid = _propuesta(db_session)
    orden_id, _ = create_orden(db_session, _entrada_pendiente(pid))
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            resolve_pending_order(
                db_session, orden_id, nuevo_estado=EstadoOrden.FILLED, resolved_at=_AHORA
            )


# --- órdenes: consultas ---


def test_expiradas_solo_incluye_pendientes_vencidas(db_session):
    p1, p2, p3 = (_propuesta(db_session, s) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"))
    vencida, _ = create_orden(db_session, _entrada_pendiente(p1, "a", expires_at=_AHORA))
    create_orden(db_session, _entrada_pendiente(p2, "b", symbol="ETHUSDT", expires_at=_AHORA + timedelta(hours=1)))
    resuelta, _ = create_orden(db_session, _entrada_pendiente(p3, "c", symbol="SOLUSDT", expires_at=_AHORA))
    resolve_pending_order(db_session, resuelta, nuevo_estado=EstadoOrden.REJECTED, resolved_at=_AHORA)

    assert [o.id for o in list_pending_orders_expired_at(db_session, _AHORA)] == [vencida]


def test_count_entry_orders_respeta_estados_y_ventana(db_session):
    p1, p2, p3 = (_propuesta(db_session, s) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"))
    create_orden(db_session, _entrada_pendiente(p1, "a"))
    id_b, _ = create_orden(db_session, _entrada_pendiente(p2, "b", symbol="ETHUSDT"))
    create_orden(db_session, _entrada_pendiente(p3, "c", symbol="SOLUSDT"))
    resolve_pending_order(db_session, id_b, nuevo_estado=EstadoOrden.REJECTED, resolved_at=_AHORA)
    db_session.flush()

    ahora_db = get_orden(db_session, id_b)
    db_session.refresh(ahora_db)
    desde = ahora_db.created_at - timedelta(minutes=5)
    hasta = ahora_db.created_at + timedelta(minutes=5)

    vigentes = [EstadoOrden.PENDING_APPROVAL, EstadoOrden.FILLED]
    assert count_entry_orders(db_session, estados=vigentes, created_from=desde, created_before=hasta) == 2
    assert count_entry_orders(db_session, estados=list(EstadoOrden), created_from=desde, created_before=hasta) == 3
    # ventana que no incluye el momento de creación
    assert count_entry_orders(
        db_session, estados=list(EstadoOrden), created_from=hasta, created_before=hasta + timedelta(days=1)
    ) == 0


def test_count_entry_orders_ignora_salidas(db_session):
    pid = _propuesta(db_session)
    pos_id, _ = create_posicion(db_session, _fila_posicion(pid))
    create_orden(
        db_session,
        OrdenRow(
            client_order_id="sal-1",
            proposal_id=pid,
            symbol="BTCUSDT",
            side=LadoOrden.SELL,
            intencion=IntencionOrden.STOP_LOSS,
            estado=EstadoOrden.FILLED,
            position_id=pos_id,
            quantity=Decimal("1"),
            execution_price=Decimal("92"),
            fee=Decimal("0.092"),
            filled_at=_AHORA,
        ),
    )
    db_session.flush()
    assert count_entry_orders(
        db_session,
        estados=list(EstadoOrden),
        created_from=datetime(2000, 1, 1, tzinfo=timezone.utc),
        created_before=datetime(2100, 1, 1, tzinfo=timezone.utc),
    ) == 0
    assert len(list_orders_for_position(db_session, pos_id)) == 1


# --- posiciones ---


def test_create_posicion_abre_con_toda_la_cantidad_restante(db_session):
    pid = _propuesta(db_session)
    pos_id, creada = create_posicion(db_session, _fila_posicion(pid))

    pos = get_posicion(db_session, pos_id)
    assert creada is True
    assert pos.status == EstadoPosicion.OPEN
    assert pos.remaining_quantity == pos.initial_quantity == Decimal("1")
    assert pos.take_profit_hit is False
    assert get_posicion_by_proposal(db_session, pid).id == pos_id


def test_reintento_de_create_posicion_no_duplica(db_session):
    pid = _propuesta(db_session)
    id1, creada1 = create_posicion(db_session, _fila_posicion(pid))
    id2, creada2 = create_posicion(db_session, _fila_posicion(pid))

    assert (creada1, creada2) == (True, False)
    assert id1 == id2
    assert count_open_posiciones(db_session) == 1


def test_otra_propuesta_del_mismo_simbolo_abierto_no_se_silencia(db_session):
    create_posicion(db_session, _fila_posicion(_propuesta(db_session)))
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            create_posicion(db_session, _fila_posicion(_propuesta(db_session)))


def test_listados_de_abiertas(db_session):
    create_posicion(db_session, _fila_posicion(_propuesta(db_session, "BTCUSDT"), "BTCUSDT"))
    create_posicion(db_session, _fila_posicion(_propuesta(db_session, "ETHUSDT"), "ETHUSDT"))

    assert count_open_posiciones(db_session) == 2
    assert {p.symbol for p in list_open_posiciones(db_session)} == {"BTCUSDT", "ETHUSDT"}
    assert get_open_posicion_by_symbol(db_session, "ETHUSDT").symbol == "ETHUSDT"
    assert get_open_posicion_by_symbol(db_session, "SOLUSDT") is None


def test_cierre_parcial_actualiza_estado_de_monitoreo_sin_tocar_lo_original(db_session):
    pid = _propuesta(db_session)
    pos_id, _ = create_posicion(db_session, _fila_posicion(pid))

    ok = update_open_position(
        db_session,
        pos_id,
        status=EstadoPosicion.OPEN,
        remaining_quantity=Decimal("0.5"),
        take_profit_hit=True,
        trailing_active=True,
        trailing_peak_price=Decimal("112"),
        closed_at=None,
    )

    assert ok is True
    pos = get_posicion(db_session, pos_id)
    db_session.refresh(pos)
    assert pos.remaining_quantity == Decimal("0.5")
    assert pos.take_profit_hit and pos.trailing_active
    assert pos.trailing_peak_price == Decimal("112")
    assert pos.initial_quantity == Decimal("1")
    assert pos.initial_stop_price == Decimal("92")
    assert pos.initial_target_price == Decimal("112")
    assert pos.entry_price == Decimal("100")
    assert pos.updated_at is not None


def test_cierre_total_y_posicion_cerrada_no_se_vuelve_a_modificar(db_session):
    pid = _propuesta(db_session)
    pos_id, _ = create_posicion(db_session, _fila_posicion(pid))

    assert update_open_position(
        db_session,
        pos_id,
        status=EstadoPosicion.CLOSED,
        remaining_quantity=Decimal(0),
        take_profit_hit=False,
        trailing_active=False,
        trailing_peak_price=None,
        closed_at=_AHORA,
    )
    # Un reintento tardío (p. ej. otro tick del monitor) no la reabre.
    assert not update_open_position(
        db_session,
        pos_id,
        status=EstadoPosicion.OPEN,
        remaining_quantity=Decimal("1"),
        take_profit_hit=False,
        trailing_active=False,
        trailing_peak_price=None,
        closed_at=None,
    )
    pos = get_posicion(db_session, pos_id)
    db_session.refresh(pos)
    assert pos.status == EstadoPosicion.CLOSED
    assert pos.remaining_quantity == 0
    assert count_open_posiciones(db_session) == 0


def test_estado_incoherente_lo_rechaza_la_base_de_datos(db_session):
    pid = _propuesta(db_session)
    pos_id, _ = create_posicion(db_session, _fila_posicion(pid))
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            update_open_position(
                db_session,
                pos_id,
                status=EstadoPosicion.CLOSED,
                remaining_quantity=Decimal("0.5"),  # CLOSED con cantidad restante
                take_profit_hit=False,
                trailing_active=False,
                trailing_peak_price=None,
                closed_at=_AHORA,
            )
