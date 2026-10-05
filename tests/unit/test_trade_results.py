"""Tests de `calculate_trade_result` y `classify_thesis` (Paso 9; sección 8.4)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.enums import EstadoTesis, IntencionOrden
from app.domain.rules.results import (
    ContextoDeEntrada,
    OrdenDeTrade,
    PuntoDeTrayectoria,
    SlippageEstimada,
    calculate_trade_result,
    classify_thesis,
)

D = Decimal
T0 = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)


def _orden(intencion, minutos, qty, price, fee):
    return OrdenDeTrade(uuid.uuid4(), intencion, T0 + timedelta(minutes=minutos), D(qty), D(price), D(fee))


def _entrada(price="100", qty="1", fee="0.1"):
    return _orden(IntencionOrden.ENTRY, 0, qty, price, fee)


def _ctx(**kw):
    base = dict(
        position_id=uuid.uuid4(),
        symbol="BTCUSDT",
        capital_at_entry=D("1000"),
        initial_stop_price=D("92"),
        initial_target_price=D("112"),
        initial_opportunity_score=D("64.6"),
    )
    base.update(kw)
    return ContextoDeEntrada(**base)


def test_stop_loss_completo_pnl_comisiones_y_r_multiple():
    entrada = _entrada()
    salida = _orden(IntencionOrden.STOP_LOSS, 10, "1", "92", "0.092")
    r = calculate_trade_result(entrada, [salida], [], _ctx())

    assert r.gross_pnl == D("-8")
    assert r.entry_fee == D("0.1") and r.exit_fee == D("0.092") and r.total_fees == D("0.192")
    assert r.net_pnl == D("-8.192")
    assert r.gross_return_pct == D("-8")
    assert r.net_return_pct == D("-8.192")
    # riesgo inicial = |100 - 92| × 1 = 8 → R = -8.192 / 8
    assert r.initial_risk_per_unit == D("8") and r.initial_risk_value == D("8")
    assert r.realized_r_multiple == D("-1.024")
    assert r.exit_reason == IntencionOrden.STOP_LOSS and r.thesis_status == EstadoTesis.INVALIDATED
    assert r.side == "LONG" and r.duration_seconds == 600
    assert r.exit_order_id == salida.order_id and r.entry_order_id == entrada.order_id
    assert r.position_value_at_entry == D("100")


def test_salidas_parciales_usan_el_flujo_real_y_promedio_ponderado():
    entrada = _entrada()
    tp = _orden(IntencionOrden.TAKE_PROFIT_PARTIAL, 5, "0.5", "112", "0.056")
    trailing = _orden(IntencionOrden.TRAILING_STOP, 30, "0.5", "112.8", "0.0564")
    r = calculate_trade_result(entrada, [trailing, tp], [], _ctx())  # fuera de orden a propósito

    assert r.exit_quantity == D("1")
    assert r.exit_price == D("112.4")  # (0.5×112 + 0.5×112.8) / 1
    assert r.gross_pnl == D("12.4")  # 56 + 56.4 - 100
    assert r.exit_fee == D("0.1124") and r.total_fees == D("0.2124")
    assert r.net_pnl == D("12.1876")
    # La orden que cerró la posición define salida, hora y motivo.
    assert r.exit_order_id == trailing.order_id
    assert r.exit_reason == IntencionOrden.TRAILING_STOP
    assert r.exit_time == trailing.filled_at and r.duration_seconds == 1800
    assert r.thesis_status == EstadoTesis.CONFIRMED


def test_el_pnl_neto_coincide_con_el_flujo_de_caja_del_equity():
    # Misma cuenta que `calculate_equity`: caja final - capital inicial.
    entrada = _entrada(price="50", qty="2", fee="0.1")
    salida = _orden(IntencionOrden.TRAILING_STOP, 3, "2", "55", "0.11")
    r = calculate_trade_result(entrada, [salida], [], _ctx(initial_stop_price=D("46")))
    caja = -(D("2") * D("50") + D("0.1")) + (D("2") * D("55") - D("0.11"))
    assert r.net_pnl == caja


def test_slippage_estimada_se_resta_y_null_cuenta_como_cero():
    entrada, salida = _entrada(), _orden(IntencionOrden.TRAILING_STOP, 1, "1", "110", "0.11")
    sin = calculate_trade_result(entrada, [salida], [], _ctx())
    assert sin.estimated_total_slippage is None and sin.net_pnl == D("9.79")

    con = calculate_trade_result(
        entrada, [salida], [], _ctx(), slippage=SlippageEstimada(D("0.05"), D("0.05"), D("0.2"))
    )
    assert con.estimated_total_slippage == D("0.2") and con.net_pnl == D("9.59")


def test_r_multiple_es_none_si_el_stop_es_igual_a_la_entrada():
    r = calculate_trade_result(
        _entrada(), [_orden(IntencionOrden.TRAILING_STOP, 1, "1", "101", "0.1")], [], _ctx(initial_stop_price=D("100"))
    )
    assert r.realized_r_multiple is None and r.initial_risk_value == D("0")


def test_benchmarks_se_copian_tal_cual():
    r = calculate_trade_result(
        _entrada(),
        [_orden(IntencionOrden.TRAILING_STOP, 1, "1", "101", "0.1")],
        [],
        _ctx(),
        btc_return_pct=D("1.5"),
        market_return_pct=D("-0.25"),
    )
    assert r.btc_return_during_trade_pct == D("1.5") and r.market_return_during_trade_pct == D("-0.25")


def test_reproducible_mismos_datos_mismo_resultado():
    entrada = _entrada()
    salidas = [_orden(IntencionOrden.STOP_LOSS, 4, "1", "92", "0.092")]
    ctx = _ctx()
    assert calculate_trade_result(entrada, salidas, [], ctx) == calculate_trade_result(entrada, salidas, [], ctx)


@pytest.mark.parametrize(
    "salidas",
    [
        [],  # sin salidas
        [("STOP_LOSS", 1, "0.5", "92", "0.05")],  # solo sale la mitad
        [("STOP_LOSS", 1, "1.5", "92", "0.05")],  # sale de más
        [("ENTRY", 1, "1", "92", "0.05")],  # una "salida" que es entrada
        [("STOP_LOSS", -1, "1", "92", "0.05")],  # anterior a la entrada
    ],
)
def test_datos_incoherentes_se_rechazan(salidas):
    objs = [_orden(IntencionOrden(i), m, q, p, f) for i, m, q, p, f in salidas]
    with pytest.raises(ValueError):
        calculate_trade_result(_entrada(), objs, [], _ctx())


def test_la_entrada_debe_ser_una_orden_de_entrada():
    with pytest.raises(ValueError):
        calculate_trade_result(
            _orden(IntencionOrden.STOP_LOSS, 0, "1", "100", "0.1"),
            [_orden(IntencionOrden.STOP_LOSS, 1, "1", "92", "0.1")],
            [],
            _ctx(),
        )


# --- thesis_status por desenlace (decisión del 2026-10-05) ---


@pytest.mark.parametrize(
    "motivo,pnl,tp,esperado",
    [
        (IntencionOrden.STOP_LOSS, "-8", False, EstadoTesis.INVALIDATED),
        (IntencionOrden.STOP_LOSS, "1", True, EstadoTesis.INVALIDATED),  # el stop manda siempre
        (IntencionOrden.TRAILING_STOP, "12", True, EstadoTesis.CONFIRMED),
        (IntencionOrden.TRAILING_STOP, "2", False, EstadoTesis.PARTIALLY_CONFIRMED),
        (IntencionOrden.TRAILING_STOP, "-1", True, EstadoTesis.INCONCLUSIVE),
        (IntencionOrden.TRAILING_STOP, "0", True, EstadoTesis.INCONCLUSIVE),
        (IntencionOrden.MANUAL_APPROVAL_EXIT, "10", True, EstadoTesis.INCONCLUSIVE),
        (IntencionOrden.SYSTEM_DRAWDOWN, "-3", False, EstadoTesis.INCONCLUSIVE),
        (IntencionOrden.RECONCILIATION_EXIT, "5", False, EstadoTesis.INCONCLUSIVE),
        (IntencionOrden.EMERGENCY_STOP, "-5", False, EstadoTesis.INCONCLUSIVE),
        (IntencionOrden.OTHER, "5", True, EstadoTesis.INCONCLUSIVE),
    ],
)
def test_classify_thesis(motivo, pnl, tp, esperado):
    assert classify_thesis(exit_reason=motivo, net_pnl=D(pnl), take_profit_hit=tp) == esperado


def test_la_entrada_debe_tener_cantidad_y_precio_positivos():
    with pytest.raises(ValueError):
        calculate_trade_result(
            _entrada(qty="0"), [_orden(IntencionOrden.STOP_LOSS, 1, "1", "92", "0.1")], [], _ctx()
        )
