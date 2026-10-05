"""Tests de `scripts.monitor` y `scripts.system_control` (la lógica de los
comandos de terminal) contra Postgres real, con precios falsos y sin dormir."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

import scripts.monitor as monitor_script
from app.repositories.equity import list_observaciones
from scripts.monitor import format_tick, run_loop, run_once
from scripts.system_control import parse_args as parse_control_args
from scripts.system_control import run_command as run_control
from tests.integration.test_monitor import CAPITAL, FakePrices, _abrir, _pausar, _t
from app.execution.monitor import run_monitor_tick

D = Decimal
_AHORA = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)


@pytest.fixture()
def usar_db_session(db_session, monkeypatch):
    """`session_scope` real hace commit sobre su propia sesión; aquí se reemplaza
    por la sesión aislada del test (sin commit real)."""

    @contextmanager
    def _scope(_factory):
        yield db_session

    monkeypatch.setattr(monitor_script, "session_scope", _scope)
    return db_session


# --- presentación ---


def test_format_tick_muestra_equity_pico_y_salidas(db_session):
    _abrir(db_session)
    res = run_monitor_tick(
        db_session, now=_t(1), price_source=FakePrices({"BTCUSDT": "92"}), initial_capital=CAPITAL
    )
    texto = "\n".join(format_tick(res, _t(1)))
    assert "equity=991.808" in texto and "pico=1000" in texto and "abiertas=0" in texto
    assert "SALIDA STOP_LOSS: BTCUSDT al precio 92" in texto


def test_format_tick_avisa_de_pausa_sin_precio_y_equity_no_calculada(db_session):
    _pausar(db_session)
    pausa = run_monitor_tick(
        db_session, now=_t(2), price_source=FakePrices({"BTCUSDT": "94.6"}), initial_capital=CAPITAL
    )
    assert pausa.pausa_registrada is False  # ya estaba pausado desde `_pausar`
    sin_precio = run_monitor_tick(db_session, now=_t(3), price_source=FakePrices({}), initial_capital=CAPITAL)
    texto = "\n".join(format_tick(sin_precio, _t(3)))
    assert "equity=no calculada" in texto and "sin precio válido para BTCUSDT" in texto


def test_format_tick_anuncia_la_pausa_cuando_se_registra(db_session):
    from tests.integration.test_monitor import _sembrar_pico

    _abrir(db_session)
    _sembrar_pico(db_session, "1170", "1170")
    res = run_monitor_tick(
        db_session, now=_t(1), price_source=FakePrices({"BTCUSDT": "94.6"}), initial_capital=CAPITAL
    )
    texto = "\n".join(format_tick(res, _t(1)))
    assert "PAUSA POR DRAWDOWN" in texto and "scripts.system_control resume" in texto


# --- run_once / códigos de salida ---


def test_run_once_codigo_0_si_todo_bien(usar_db_session):
    lineas, codigo = run_once(None, price_source=FakePrices({}), initial_capital=CAPITAL, now=_AHORA)
    assert codigo == 0 and "equity=1000" in lineas[0]


def test_run_once_codigo_1_si_falta_un_precio(usar_db_session):
    _abrir(usar_db_session)
    lineas, codigo = run_once(None, price_source=FakePrices({}), initial_capital=CAPITAL, now=_AHORA)
    assert codigo == 1 and any("sin precio válido" in l for l in lineas)


def test_run_once_codigo_2_ante_una_discrepancia_y_no_toca_nada(usar_db_session):
    _abrir(usar_db_session)
    from app.repositories.positions import list_open_posiciones

    pos = list_open_posiciones(usar_db_session)[0]
    usar_db_session.execute(text("UPDATE posiciones SET remaining_quantity = 0.5 WHERE id = :id"), {"id": pos.id})
    usar_db_session.expire_all()

    lineas, codigo = run_once(None, price_source=FakePrices({"BTCUSDT": "50"}), initial_capital=CAPITAL, now=_AHORA)
    assert codigo == 2
    assert "CANTIDAD_RESTANTE_NO_COINCIDE" in "\n".join(lineas) and "SIN actuar" in lineas[0]
    assert list_observaciones(usar_db_session) == []


# --- el bucle ---


def _reloj():
    t = {"n": 0}

    def clock():
        t["n"] += 1
        return _AHORA + timedelta(minutes=t["n"])

    return clock


def test_el_bucle_repite_el_chequeo_con_el_intervalo_pedido(usar_db_session):
    dormidos: list[float] = []
    salida: list[str] = []
    codigo = run_loop(
        None,
        price_source=FakePrices({}),
        initial_capital=CAPITAL,
        interval=60,
        sleep=dormidos.append,
        clock=_reloj(),
        max_ticks=3,
        emit=salida.append,
    )
    assert codigo == 0
    assert len(list_observaciones(usar_db_session)) == 3
    assert len(dormidos) == 2 and all(0 <= d <= 60 for d in dormidos)  # no duerme tras el último


def test_el_bucle_sigue_tras_un_fallo_inesperado_y_se_detiene_solo_por_reconciliacion(usar_db_session):
    class PreciosRotos:
        def __init__(self):
            self.llamadas = 0

        def get_price(self, symbol):
            self.llamadas += 1
            if self.llamadas == 1:
                raise RuntimeError("fallo inesperado de la fuente")  # no es PriceUnavailable
            return D("100")

    _abrir(usar_db_session)
    salida: list[str] = []
    codigo = run_loop(
        None,
        price_source=PreciosRotos(),
        initial_capital=CAPITAL,
        sleep=lambda _s: None,
        clock=_reloj(),
        max_ticks=3,
        emit=salida.append,
    )
    assert codigo == 0
    assert any("ERROR: el chequeo falló" in l for l in salida)  # el primero falló y se reintentó
    assert sum("equity=" in l and "no calculada" not in l for l in salida) == 2  # los otros dos funcionaron


def test_el_bucle_se_detiene_con_codigo_2_ante_una_discrepancia(usar_db_session):
    _abrir(usar_db_session)
    from app.repositories.positions import list_open_posiciones

    pos = list_open_posiciones(usar_db_session)[0]
    usar_db_session.execute(text("UPDATE posiciones SET remaining_quantity = 0.5 WHERE id = :id"), {"id": pos.id})
    usar_db_session.expire_all()
    dormidos: list[float] = []
    codigo = run_loop(
        None,
        price_source=FakePrices({"BTCUSDT": "100"}),
        initial_capital=CAPITAL,
        sleep=dormidos.append,
        clock=_reloj(),
        max_ticks=5,
        emit=lambda _l: None,
    )
    assert codigo == 2 and dormidos == []


# --- system_control ---


def _control(db_session, comando, precios=None):
    return run_control(
        db_session,
        parse_control_args([comando]),
        price_source=FakePrices(precios or {}),
        now=_t(10),
        initial_capital=CAPITAL,
    )


def test_status_en_un_sistema_nuevo(db_session):
    texto = "\n".join(_control(db_session, "status"))
    assert "Pausado por drawdown: no" in texto and "Archivo PARAR: no" in texto
    assert "ninguno todavía" in texto and "Pico de equity vigente: 1000" in texto


def test_status_con_pausa_y_con_parar(db_session):
    from app.execution import emergency_stop

    _pausar(db_session)
    emergency_stop.PARAR_PATH.write_text("")
    texto = "\n".join(_control(db_session, "status"))
    assert "Pausado por drawdown: SÍ" in texto and "PRESENTE" in texto
    assert "Último equity registrado: 994.5" in texto and "Pico de equity vigente: 1170" in texto


def test_resume_reanuda_e_informa_el_equity(db_session):
    _pausar(db_session)
    texto = "\n".join(_control(db_session, "resume", {"BTCUSDT": "94.6"}))
    assert texto.startswith("REANUDADO") and "994.5" in texto
    assert "Pausado por drawdown: no" in "\n".join(_control(db_session, "status"))


def test_resume_sin_estar_pausado_y_sin_precio(db_session):
    assert "no estaba pausado" in "\n".join(_control(db_session, "resume"))
    _pausar(db_session)
    assert "SIN_PRECIO" in "\n".join(_control(db_session, "resume", {}))
