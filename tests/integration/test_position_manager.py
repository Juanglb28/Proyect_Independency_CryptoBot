"""Tests de los cierres de posiciones (`app/execution/position_manager.py`)
contra Postgres real: stop, take profit parcial y trailing en sus umbrales
EXACTOS, precio de salida = precio observado, atomicidad, idempotencia y que
la decisión original no se toque."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.enums import (
    EstadoCiclo,
    EstadoOrden,
    EstadoPosicion,
    IntencionOrden,
    LadoOrden,
    TierEntrada,
)
from app.domain.rules.execution import CandidatoEntrada
from app.domain.rules.risk import AccionMonitor
from app.execution.order_manager import open_entries
from app.execution.position_manager import check_position, exit_client_order_id
from app.execution.price_source import PriceUnavailable
from app.repositories.analysis import create_ciclo, get_propuesta, save_propuesta
from app.repositories.orders import OrdenRow, create_orden, list_orders_for_position
from app.repositories.positions import get_open_posicion_by_symbol, get_posicion
from tests.integration.test_analysis_repository import _propuesta_row

D = Decimal
_AHORA = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
_DESPUES = _AHORA + timedelta(minutes=1)


class FakePrices:
    def __init__(self, precios: dict[str, str]):
        self.precios = {k: D(v) for k, v in precios.items()}

    def get_price(self, symbol: str) -> Decimal:
        if symbol not in self.precios:
            raise PriceUnavailable(symbol)
        return self.precios[symbol]


def _abrir(db_session, symbol="BTCUSDT", precio="100"):
    """Abre una posición NORMAL de 100 USD al `precio`: con precio 100 la
    cantidad es 1, el stop 92 y el objetivo 112."""
    ciclo = create_ciclo(db_session, started_at=_AHORA, status=EstadoCiclo.PREFILTER)
    row = dataclasses.replace(
        _propuesta_row(ciclo, symbol), tier_entrada=TierEntrada.NORMAL, puntaje_ponderado=D("64.60")
    )
    pid = save_propuesta(db_session, row)
    cand = CandidatoEntrada(pid, symbol, TierEntrada.NORMAL, D("64.60"))
    open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({symbol: precio}))
    return get_open_posicion_by_symbol(db_session, symbol), pid


def _salidas(db_session, posicion):
    return [o for o in list_orders_for_position(db_session, posicion.id) if o.side == LadoOrden.SELL]


# --- stop loss en el umbral exacto ---


def test_stop_exactamente_en_menos_8_por_ciento_cierra_todo(db_session):
    pos, _ = _abrir(db_session)
    res = check_position(db_session, pos, D("92"), now=_DESPUES)

    assert res.accion == AccionMonitor.CERRAR_TOTAL and res.intencion == IntencionOrden.STOP_LOSS
    (venta,) = _salidas(db_session, pos)
    assert venta.estado == EstadoOrden.FILLED and venta.intencion == IntencionOrden.STOP_LOSS
    assert venta.quantity == D("1") and venta.execution_price == D("92") and venta.fee == D("0.092")
    assert venta.client_order_id == exit_client_order_id(pos.id, IntencionOrden.STOP_LOSS)
    assert venta.filled_at == _DESPUES and venta.position_id == pos.id

    pos = get_posicion(db_session, pos.id)
    assert pos.status == EstadoPosicion.CLOSED
    assert pos.remaining_quantity == 0 and pos.closed_at == _DESPUES
    assert pos.take_profit_hit is False and pos.trailing_active is False


def test_un_centavo_sobre_el_stop_no_cierra(db_session):
    pos, _ = _abrir(db_session)
    res = check_position(db_session, pos, D("92.01"), now=_DESPUES)
    assert res.accion == AccionMonitor.MANTENER and res.orden_id is None
    assert _salidas(db_session, pos) == []
    assert get_posicion(db_session, pos.id).status == EstadoPosicion.OPEN


def test_el_stop_sale_al_precio_observado_no_al_nivel(db_session):
    pos, _ = _abrir(db_session)
    check_position(db_session, pos, D("80"), now=_DESPUES)  # saltó de -7% a -20% entre chequeos
    (venta,) = _salidas(db_session, pos)
    assert venta.execution_price == D("80") and venta.reference_price == D("80")
    assert venta.fee == D("0.08")


# --- take profit parcial en el umbral exacto ---


def test_objetivo_exactamente_en_mas_12_por_ciento_vende_la_mitad_y_activa_trailing(db_session):
    pos, _ = _abrir(db_session)
    res = check_position(db_session, pos, D("112"), now=_DESPUES)

    assert res.accion == AccionMonitor.CERRAR_PARCIAL and res.intencion == IntencionOrden.TAKE_PROFIT_PARTIAL
    (venta,) = _salidas(db_session, pos)
    assert venta.quantity == D("0.5") and venta.execution_price == D("112") and venta.fee == D("0.056")

    pos = get_posicion(db_session, pos.id)
    assert pos.status == EstadoPosicion.OPEN and pos.closed_at is None
    assert pos.remaining_quantity == D("0.5")
    assert pos.take_profit_hit is True and pos.trailing_active is True
    assert pos.trailing_peak_price == D("112")


def test_un_centavo_bajo_el_objetivo_no_vende(db_session):
    pos, _ = _abrir(db_session)
    res = check_position(db_session, pos, D("111.99"), now=_DESPUES)
    assert res.accion == AccionMonitor.MANTENER
    assert _salidas(db_session, pos) == []
    assert get_posicion(db_session, pos.id).take_profit_hit is False


# --- trailing en el umbral exacto ---


def _con_tp_ejecutado(db_session):
    pos, pid = _abrir(db_session)
    check_position(db_session, pos, D("112"), now=_DESPUES)
    return get_posicion(db_session, pos.id), pid


def test_el_maximo_del_trailing_solo_sube(db_session):
    pos, _ = _con_tp_ejecutado(db_session)
    check_position(db_session, pos, D("120"), now=_DESPUES)
    assert get_posicion(db_session, pos.id).trailing_peak_price == D("120")

    res = check_position(db_session, pos, D("115"), now=_DESPUES)  # retrocede, pero no 6%
    assert res.accion == AccionMonitor.MANTENER
    assert get_posicion(db_session, pos.id).trailing_peak_price == D("120")  # no baja
    assert len(_salidas(db_session, pos)) == 1  # solo el TP parcial


def test_trailing_exactamente_en_6_por_ciento_desde_el_maximo_cierra_el_resto(db_session):
    pos, _ = _con_tp_ejecutado(db_session)
    check_position(db_session, pos, D("120"), now=_DESPUES + timedelta(minutes=1))  # nivel: 120 × 0,94 = 112,8

    res = check_position(db_session, pos, D("112.8"), now=_DESPUES + timedelta(minutes=2))
    assert res.accion == AccionMonitor.CERRAR_TOTAL and res.intencion == IntencionOrden.TRAILING_STOP

    ventas = _salidas(db_session, pos)
    assert [v.intencion for v in ventas] == [IntencionOrden.TAKE_PROFIT_PARTIAL, IntencionOrden.TRAILING_STOP]
    assert ventas[1].quantity == D("0.5") and ventas[1].execution_price == D("112.8")

    pos = get_posicion(db_session, pos.id)
    assert pos.status == EstadoPosicion.CLOSED and pos.remaining_quantity == 0
    assert pos.trailing_peak_price == D("120")  # el máximo final queda como registro


def test_un_centavo_sobre_el_nivel_del_trailing_no_cierra(db_session):
    pos, _ = _con_tp_ejecutado(db_session)
    check_position(db_session, pos, D("120"), now=_DESPUES)
    res = check_position(db_session, pos, D("112.81"), now=_DESPUES)
    assert res.accion == AccionMonitor.MANTENER
    assert get_posicion(db_session, pos.id).status == EstadoPosicion.OPEN


def test_una_posicion_con_tp_ejecutado_no_vende_otra_vez_al_cruzar_el_objetivo(db_session):
    pos, _ = _con_tp_ejecutado(db_session)
    res = check_position(db_session, pos, D("113"), now=_DESPUES)  # sigue sobre el objetivo
    assert res.accion == AccionMonitor.MANTENER
    assert len(_salidas(db_session, pos)) == 1


# --- idempotencia y atomicidad ---


def test_reintentar_el_chequeo_tras_un_cierre_no_vende_dos_veces(db_session):
    pos, _ = _abrir(db_session)
    check_position(db_session, pos, D("92"), now=_DESPUES)
    # Un reintento (mismo objeto, ya cerrado) se rechaza antes de tocar nada.
    with pytest.raises(ValueError):
        check_position(db_session, pos, D("92"), now=_DESPUES)
    assert len(_salidas(db_session, pos)) == 1


def test_una_orden_de_salida_previa_con_el_mismo_id_revierte_todo(db_session):
    pos, pid = _abrir(db_session)
    # Estado incoherente: ya existe la orden de stop pero la posición sigue abierta.
    create_orden(
        db_session,
        OrdenRow(
            client_order_id=exit_client_order_id(pos.id, IntencionOrden.STOP_LOSS),
            proposal_id=pid,
            symbol="BTCUSDT",
            side=LadoOrden.SELL,
            intencion=IntencionOrden.STOP_LOSS,
            estado=EstadoOrden.FILLED,
            position_id=pos.id,
            quantity=D("1"),
            execution_price=D("92"),
            fee=D("0.092"),
            filled_at=_AHORA,
        ),
    )
    with pytest.raises(RuntimeError, match="incoherente"):
        with db_session.begin_nested():
            check_position(db_session, pos, D("92"), now=_DESPUES)
    pos = get_posicion(db_session, pos.id)
    assert pos.status == EstadoPosicion.OPEN and pos.remaining_quantity == D("1")
    assert len(_salidas(db_session, pos)) == 1  # solo la preexistente


def test_un_fallo_a_mitad_deja_la_posicion_y_las_ordenes_como_estaban(db_session, monkeypatch):
    from app.execution import position_manager as pm

    pos, _ = _abrir(db_session)

    def falla(*args, **kwargs):
        raise RuntimeError("caída simulada entre la orden y la posición")

    monkeypatch.setattr(pm, "update_open_position", falla)
    with pytest.raises(RuntimeError, match="caída simulada"):
        with db_session.begin_nested():
            check_position(db_session, pos, D("92"), now=_DESPUES)

    assert _salidas(db_session, pos) == []  # la venta se revirtió junto con todo
    assert get_posicion(db_session, pos.id).status == EstadoPosicion.OPEN


# --- la decisión original no se toca ---


def test_el_decision_snapshot_y_los_niveles_iniciales_no_cambian_durante_toda_la_vida(db_session):
    pos, pid = _abrir(db_session)
    snapshot_antes = dict(get_propuesta(db_session, pid).decision_snapshot)
    niveles = (pos.entry_price, pos.initial_quantity, pos.initial_stop_price, pos.initial_target_price)

    check_position(db_session, pos, D("112"), now=_DESPUES)
    check_position(db_session, pos, D("120"), now=_DESPUES)
    check_position(db_session, pos, D("112.8"), now=_DESPUES)

    pos = get_posicion(db_session, pos.id)
    assert pos.status == EstadoPosicion.CLOSED
    assert (pos.entry_price, pos.initial_quantity, pos.initial_stop_price, pos.initial_target_price) == niveles
    assert get_propuesta(db_session, pid).decision_snapshot == snapshot_antes


def test_now_sin_zona_horaria_se_rechaza(db_session):
    pos, _ = _abrir(db_session)
    with pytest.raises(ValueError):
        check_position(db_session, pos, D("100"), now=datetime(2026, 10, 5, 15, 0))
