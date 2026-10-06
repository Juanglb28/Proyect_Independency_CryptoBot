"""Acciones manuales de la interfaz (Paso 10c) contra Postgres real:
aprobar, rechazar y reanudar. Reutilizan las funciones deterministas de
`app.execution`; aquí se comprueba el envoltorio (mensajes, transacción,
errores) y que las reglas de bloqueo se respeten también desde la interfaz.
"""

from __future__ import annotations

import dataclasses
import uuid
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

import pytest

from app.domain.enums import EstadoCiclo, EstadoOrden, IntencionOrden, TierEntrada, TipoEventoSistema
from app.domain.rules.execution import CandidatoEntrada
from app.execution.order_manager import open_entries
from app.execution.system_control import get_system_state
from app.repositories.analysis import create_ciclo, save_propuesta
from app.repositories.orders import get_orden, list_orders
from app.repositories.positions import get_open_posicion_by_symbol
from app.repositories.system_events import EventoSistemaRow, append_evento_sistema, get_ultimo_evento_sistema
from app.services import commands
from tests.integration import test_monitor as monitor_helpers
from tests.integration.test_analysis_repository import _propuesta_row
from tests.integration.test_results_pipeline import CAPITAL, FakePrices, _t, _universo

D = Decimal


@pytest.fixture
def acciones(db_session, monkeypatch):
    """Las acciones escriben en la sesión de prueba, con hora y precios fijos."""
    estado = {"ahora": _t(5), "precios": FakePrices(_universo())}

    @contextmanager
    def _sesion():
        yield db_session

    monkeypatch.setattr(commands, "_abrir_sesion", _sesion)
    monkeypatch.setattr(commands, "_ahora", lambda: estado["ahora"])
    monkeypatch.setattr(commands, "_fuente_de_precios", lambda: estado["precios"])
    return estado


def _pendiente(db_session, symbol="ETHUSDT") -> uuid.UUID:
    """Una entrada MAYOR_RIESGO: siempre queda pendiente de aprobación."""
    ciclo = create_ciclo(db_session, started_at=_t(0), status=EstadoCiclo.PREFILTER)
    row = dataclasses.replace(
        _propuesta_row(ciclo, symbol), tier_entrada=TierEntrada.MAYOR_RIESGO, puntaje_ponderado=D("52")
    )
    pid = save_propuesta(db_session, row)
    open_entries(db_session, [CandidatoEntrada(pid, symbol, TierEntrada.MAYOR_RIESGO, D("52"))],
                 now=_t(0), price_source=FakePrices({}))
    (orden,) = list_orders(db_session, estados=[EstadoOrden.PENDING_APPROVAL], intencion=IntencionOrden.ENTRY)
    return orden.id


def _pausar_sin_posiciones(db_session):
    append_evento_sistema(
        db_session,
        EventoSistemaRow(event_type=TipoEventoSistema.PAUSA_DRAWDOWN, occurred_at=_t(1),
                         equity_usd=D("850"), peak_equity_usd=D("1000")),
    )


# --- aprobar ---


def test_aprobar_abre_la_posicion_al_precio_de_ahora(db_session, acciones):
    oid = _pendiente(db_session)
    acciones["precios"] = FakePrices(_universo(ETHUSDT="105"))
    r = commands.aprobar_entrada(oid)
    assert (r.ok, r.codigo) == (True, "APROBADA") and "ejecutada en paper" in r.mensaje
    assert get_orden(db_session, oid).estado == EstadoOrden.FILLED
    assert get_open_posicion_by_symbol(db_session, "ETHUSDT").entry_price == D("105")


def test_aprobar_dos_veces_no_duplica(db_session, acciones):
    oid = _pendiente(db_session)
    assert commands.aprobar_entrada(oid).ok is True
    r = commands.aprobar_entrada(oid)
    assert (r.ok, r.codigo) == (False, "NO_PENDIENTE")
    assert len(list_orders(db_session, estados=[EstadoOrden.FILLED], intencion=IntencionOrden.ENTRY)) == 1


def test_aprobar_sin_precio_deja_la_orden_pendiente(db_session, acciones):
    oid = _pendiente(db_session)
    acciones["precios"] = FakePrices({})
    r = commands.aprobar_entrada(oid)
    assert (r.ok, r.codigo) == (False, "SIN_PRECIO")
    assert get_orden(db_session, oid).estado == EstadoOrden.PENDING_APPROVAL
    assert get_open_posicion_by_symbol(db_session, "ETHUSDT") is None


def test_aprobar_con_el_sistema_pausado_se_bloquea(db_session, acciones):
    oid = _pendiente(db_session)
    _pausar_sin_posiciones(db_session)
    r = commands.aprobar_entrada(oid)
    assert (r.ok, r.codigo) == (False, "BLOQUEADA_POR_PAUSA") and "puedes rechazarla" in r.mensaje
    assert get_orden(db_session, oid).estado == EstadoOrden.PENDING_APPROVAL


def test_aprobar_una_entrada_vencida_la_expira(db_session, acciones):
    oid = _pendiente(db_session)
    acciones["ahora"] = _t(0, 2)  # dos días después: ya venció (vence al terminar el día UTC)
    r = commands.aprobar_entrada(oid)
    assert (r.ok, r.codigo) == (False, "EXPIRADA")
    assert get_orden(db_session, oid).estado == EstadoOrden.EXPIRED


# --- rechazar ---


def test_rechazar_libera_la_entrada_y_guarda_el_motivo(db_session, acciones):
    oid = _pendiente(db_session)
    r = commands.rechazar_entrada(oid, "  no me convence  ")
    assert (r.ok, r.codigo) == (True, "RECHAZADA")
    orden = get_orden(db_session, oid)
    assert orden.estado == EstadoOrden.REJECTED and orden.detail == "no me convence"


def test_rechazar_sin_motivo_usa_el_texto_por_defecto_y_es_posible_en_pausa(db_session, acciones):
    oid = _pendiente(db_session)
    _pausar_sin_posiciones(db_session)
    assert commands.rechazar_entrada(oid, "   ").ok is True
    assert get_orden(db_session, oid).detail == "Rechazada manualmente."


def test_rechazar_dos_veces(db_session, acciones):
    oid = _pendiente(db_session)
    commands.rechazar_entrada(oid)
    r = commands.rechazar_entrada(oid)
    assert (r.ok, r.codigo) == (False, "NO_PENDIENTE")


# --- reanudar ---


def test_reanudar_sin_pausa_no_hace_nada(db_session, acciones):
    r = commands.reanudar_sistema()
    assert (r.ok, r.codigo) == (False, "NO_PAUSADO")


def test_reanudar_reinicia_el_pico_y_lo_dice(db_session, acciones):
    _pausar_sin_posiciones(db_session)
    r = commands.reanudar_sistema()
    assert (r.ok, r.codigo) == (True, "REANUDADO") and "1000.00 USD" in r.mensaje
    assert get_ultimo_evento_sistema(db_session).event_type == TipoEventoSistema.REANUDACION
    estado = get_system_state(db_session, initial_capital=CAPITAL)
    assert estado.pausado is False and estado.pico_vigente == D("1000")


def test_reanudar_con_posicion_abierta_usa_su_precio(db_session, acciones):
    monitor_helpers._pausar(db_session)
    acciones["ahora"] = monitor_helpers._t(5)
    acciones["precios"] = monitor_helpers.FakePrices({"BTCUSDT": "94.6"})
    r = commands.reanudar_sistema()
    assert r.ok is True and "994.50 USD" in r.mensaje


def test_reanudar_sin_precio_no_reanuda(db_session, acciones):
    monitor_helpers._pausar(db_session)
    acciones["ahora"] = monitor_helpers._t(5)
    acciones["precios"] = monitor_helpers.FakePrices({})
    r = commands.reanudar_sistema()
    assert (r.ok, r.codigo) == (False, "SIN_PRECIO")
    assert get_system_state(db_session, initial_capital=CAPITAL).pausado is True


# --- errores ---


def test_un_error_inesperado_devuelve_un_mensaje_claro_sin_traza(db_session, acciones):
    r = commands.aprobar_entrada(uuid.uuid4())  # la orden no existe -> ValueError
    assert (r.ok, r.codigo) == (False, "ERROR")
    assert "no se cambió nada" in r.mensaje and "Traceback" not in r.mensaje
    assert commands.rechazar_entrada(uuid.uuid4()).codigo == "ERROR"
