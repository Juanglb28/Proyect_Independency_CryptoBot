"""MAE/MFE de un trade a partir de su trayectoria (sección 8.4)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.domain.enums import IntencionOrden
from app.domain.rules.results import ContextoDeEntrada, OrdenDeTrade, PuntoDeTrayectoria, calculate_trade_result

D = Decimal
T0 = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)


def _t(minutos):
    return T0 + timedelta(minutes=minutos)


def _resultado(precios, *, entry="100", qty="2", salida_min=10):
    entrada = OrdenDeTrade(uuid.uuid4(), IntencionOrden.ENTRY, T0, D(qty), D(entry), D("0.1"))
    salida = OrdenDeTrade(uuid.uuid4(), IntencionOrden.TRAILING_STOP, _t(salida_min), D(qty), D("105"), D("0.1"))
    ctx = ContextoDeEntrada(position_id=uuid.uuid4(), symbol="BTCUSDT", capital_at_entry=D("1000"),
                            initial_stop_price=D("92"))
    puntos = [PuntoDeTrayectoria(_t(m), D(p)) for m, p in precios]
    return calculate_trade_result(entrada, [salida], puntos, ctx)


def test_mae_y_mfe_salen_del_minimo_y_maximo_observados():
    r = _resultado([(0, "100"), (2, "95"), (4, "99"), (6, "110"), (8, "104"), (10, "105")])
    assert r.mae_pct == D("-5") and r.mfe_pct == D("10")
    # valores en USD sobre la cantidad de entrada (2 unidades)
    assert r.mae_value == D("-10") and r.mfe_value == D("20")


def test_sin_snapshots_no_se_inventan():
    r = _resultado([])
    assert r.mae_pct is r.mfe_pct is r.mae_value is r.mfe_value is None


def test_los_snapshots_fuera_de_la_vida_del_trade_se_ignoran():
    # El de t=-5 (antes de entrar) y el de t=30 (después de salir) no cuentan.
    r = _resultado([(-5, "50"), (0, "100"), (5, "102"), (10, "105"), (30, "500")])
    assert r.mae_pct == D("0") and r.mfe_pct == D("5")


def test_trade_que_solo_sube_tiene_mae_cero_con_el_snapshot_de_entrada():
    r = _resultado([(0, "100"), (5, "103"), (10, "105")])
    assert r.mae_pct == D("0") and r.mae_value == D("0")
