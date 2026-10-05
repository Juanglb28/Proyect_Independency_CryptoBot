"""Casos de borde de los servicios de resultados: benchmark de periodo con
velas reales, errores controlados y filtros de consulta."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.domain.enums import EstadoSnapshot
from app.execution import results_recorder
from app.repositories.candles import save_candles
from app.repositories.positions import get_posicion, list_open_posiciones
from app.repositories.snapshots import list_snapshots_for_position
from app.repositories.trade_results import list_trade_results, save_trade_result
from app.results import trade_result_service
from app.results.benchmark_service import calculate_period_benchmark
from app.results.performance_service import recalculate_performance
from app.results.snapshot_service import EstadoParaSnapshot, record_snapshot
from app.results.trade_result_service import ensure_trade_results, generate_trade_result
from tests.integration.test_results_pipeline import CAPITAL, _abrir, _t, _tick, _universo

D = Decimal
INICIO = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def _velas(cierres, symbol="BTCUSDT"):
    return [
        {
            "symbol": symbol, "timeframe": "1h", "open_time": INICIO + timedelta(hours=i),
            "open": D(c), "high": D(c), "low": D(c), "close": D(c), "volume": D("1"),
        }
        for i, c in enumerate(cierres)
    ]


# --- benchmark de periodo ---


def test_benchmark_de_periodo_usa_cierres_de_velas_ya_cerradas_dentro_de_la_ventana(db_session):
    save_candles(db_session, _velas(["100", "120", "90", "110", "999"]))
    # La vela i cierra a INICIO + (i+1) h. Ventana [01:00, 04:00]: velas 0..3 (cierran 01:00..04:00).
    b = calculate_period_benchmark(
        db_session, period_start=INICIO + timedelta(hours=1), period_end=INICIO + timedelta(hours=4)
    )
    assert b.return_pct == D("10") and b.max_drawdown_pct == D("-25")  # la vela de 999 cierra a las 05:00: fuera


def test_benchmark_de_periodo_sin_velas_suficientes_no_existe(db_session):
    save_candles(db_session, _velas(["100"]))
    b = calculate_period_benchmark(db_session, period_start=INICIO, period_end=INICIO + timedelta(hours=1))
    assert b.return_pct is None and b.max_drawdown_pct is None
    b = calculate_period_benchmark(db_session, period_start=INICIO + timedelta(days=9), period_end=INICIO + timedelta(days=10))
    assert b.return_pct is None


def test_el_benchmark_de_periodo_llega_a_las_metricas_globales(db_session):
    save_candles(db_session, _velas(["100", "110"]))
    (g,) = recalculate_performance(
        db_session, period_start=INICIO + timedelta(hours=1), period_end=INICIO + timedelta(hours=2),
        initial_capital=CAPITAL, now=INICIO + timedelta(days=1),
    )
    assert g.benchmark_return_pct == D("10") and g.benchmark_max_drawdown_pct == D("0")


# --- errores controlados ---


def test_generar_resultado_de_una_posicion_inexistente_falla(db_session):
    with pytest.raises(ValueError, match="No existe"):
        generate_trade_result(db_session, uuid.uuid4(), initial_capital=CAPITAL)


def test_una_posicion_cerrada_sin_entrada_ejecutada_es_un_estado_incoherente(db_session, monkeypatch):
    pos = _abrir(db_session)
    _tick(db_session, _universo(BTCUSDT="92"))
    db_session.execute(text("ALTER TABLE trade_results DISABLE TRIGGER trg_trade_results_solo_agregar"))
    db_session.execute(text("DELETE FROM trade_results"))
    monkeypatch.setattr(trade_result_service, "list_orders_for_position", lambda *a, **k: [])
    with pytest.raises(RuntimeError, match="exactamente una entrada"):
        generate_trade_result(db_session, pos.id, initial_capital=CAPITAL)


def test_el_barrido_cuenta_los_errores_y_sigue(db_session, monkeypatch):
    pos = _abrir(db_session)
    monkeypatch.setattr(results_recorder, "generate_trade_result", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    _tick(db_session, _universo(BTCUSDT="92"))
    monkeypatch.setattr(trade_result_service, "generate_trade_result", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("y")))
    assert ensure_trade_results(db_session, initial_capital=CAPITAL) == (0, 1)
    monkeypatch.undo()
    assert ensure_trade_results(db_session, initial_capital=CAPITAL) == (1, 0)
    assert ensure_trade_results(db_session, initial_capital=CAPITAL) == (0, 0)  # ya no queda nada por reparar
    assert get_posicion(db_session, pos.id) is not None


def test_snapshot_de_pausa_ignora_posiciones_sin_precio_y_no_bloquea_si_falla(db_session, monkeypatch):
    pos = _abrir(db_session)
    abiertas = list_open_posiciones(db_session)
    results_recorder.record_pause(db_session, abiertas, {}, now=_t(1))  # sin precio: no escribe nada
    assert [s.system_state for s in list_snapshots_for_position(db_session, pos.id)] == [EstadoSnapshot.ENTRY]
    monkeypatch.setattr(results_recorder, "record_snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    results_recorder.record_pause(db_session, abiertas, {"BTCUSDT": D("100")}, now=_t(1))  # falla y se traga
    assert len(list_snapshots_for_position(db_session, pos.id)) == 1


def test_fechas_sin_zona_horaria_se_rechazan(db_session):
    pos = _abrir(db_session)
    with pytest.raises(ValueError):
        record_snapshot(
            db_session,
            estado=EstadoParaSnapshot(pos.id, "BTCUSDT", D("100"), D("1"), False, None),
            system_state=EstadoSnapshot.SCHEDULED, snapshot_time=datetime(2026, 10, 5, 15, 1),
            market_price=D("100"), stop_price=None, target_price=None,
        )
    with pytest.raises(ValueError):
        recalculate_performance(
            db_session, period_start=datetime(2026, 10, 1), period_end=_t(0), initial_capital=CAPITAL, now=_t(0)
        )


def test_guardar_un_resultado_con_columnas_desconocidas_falla(db_session):
    with pytest.raises(ValueError):
        save_trade_result(db_session, {"position_id": uuid.uuid4(), "inventada": 1})


def test_filtros_de_resultados_por_salida_y_simbolo(db_session):
    _abrir(db_session, symbol="BTCUSDT")
    _tick(db_session, _universo(BTCUSDT="92"), minutos=5)
    _abrir(db_session, symbol="ETHUSDT", dia=1)
    _tick(db_session, _universo(ETHUSDT="92"), minutos=5, dia=1)
    assert [r.symbol for r in list_trade_results(db_session)] == ["BTCUSDT", "ETHUSDT"]
    assert [r.symbol for r in list_trade_results(db_session, symbol="ETHUSDT")] == ["ETHUSDT"]
    assert [r.symbol for r in list_trade_results(db_session, exit_desde=_t(0, 1))] == ["ETHUSDT"]
    assert [r.symbol for r in list_trade_results(db_session, exit_hasta=_t(10))] == ["BTCUSDT"]
