"""Métricas de rendimiento, drawdown máximo y benchmark (sección 8.6)."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.enums import AlcanceMetricas, EstadoTesis, IntencionOrden
from app.domain.rules.results import (
    BenchmarkPeriodo,
    PuntoDeSerie,
    TradeResult,
    average_return_pct,
    calculate_benchmark,
    calculate_max_drawdown,
    calculate_performance,
    calculate_return_pct,
)

D = Decimal
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _trade(net_pnl, *, dia=0, score="60", symbol="BTCUSDT", r=None, mae=None, mfe=None, fees="0.2", gross=None):
    """Trade de 100 USD con el P&L neto indicado (retorno % = pnl)."""
    net = D(net_pnl)
    return TradeResult(
        position_id=uuid.uuid4(), entry_order_id=uuid.uuid4(), exit_order_id=uuid.uuid4(), symbol=symbol,
        side="LONG", entry_time=T0 + timedelta(days=dia), exit_time=T0 + timedelta(days=dia, hours=1),
        duration_seconds=3600, entry_quantity=D(1), exit_quantity=D(1), entry_price=D(100), exit_price=D(100),
        capital_at_entry=D(1000), position_value_at_entry=D(100),
        gross_pnl=D(gross) if gross is not None else net + D(fees), gross_return_pct=net + D(fees),
        entry_fee=D(fees) / 2, exit_fee=D(fees) / 2, total_fees=D(fees),
        estimated_entry_slippage_pct=None, estimated_exit_slippage_pct=None, estimated_total_slippage=None,
        net_pnl=net, net_return_pct=net, initial_stop_price=D(92), initial_target_price=D(112),
        initial_risk_per_unit=D(8), initial_risk_value=D(8),
        realized_r_multiple=None if r is None else D(r),
        mae_pct=None if mae is None else D(mae), mae_value=None, mfe_pct=None if mfe is None else D(mfe),
        mfe_value=None, analysis_cycle_id=None, proposal_id=None,
        initial_opportunity_score=None if score is None else D(score), initial_risk_score=None,
        initial_confidence_score=None, thesis_status=EstadoTesis.INCONCLUSIVE,
        exit_reason=IntencionOrden.STOP_LOSS, btc_return_during_trade_pct=None, market_return_during_trade_pct=None,
    )


def _perf(trades, curve=(), bench=None):
    return calculate_performance(
        trades, curve, bench, scope_type=AlcanceMetricas.GLOBAL.value, scope_key=None,
        period_start=T0, period_end=T0 + timedelta(days=30), initial_capital=D(1000),
    )


# --- agregados básicos ---


def test_agregados_con_ganadoras_perdedoras_y_equilibrio():
    m = _perf([_trade("10", r="1.25"), _trade("4", r="0.5"), _trade("-6", r="-0.75"), _trade("0", r="0")])
    assert (m.total_trades, m.winning_trades, m.losing_trades, m.breakeven_trades) == (4, 2, 1, 1)
    assert m.win_rate_pct == D("50")
    assert m.net_pnl == D("8") and m.gross_pnl == D("8.8") and m.total_fees == D("0.8")
    assert m.total_return_pct == D("0.8")  # 8 sobre 1000
    assert m.average_trade_return_pct == D("2") and m.median_trade_return_pct == D("2")
    assert m.average_winner_pct == D("7") and m.average_loser_pct == D("-6")
    assert m.largest_win_pct == D("10") and m.largest_loss_pct == D("-6")
    assert m.profit_factor == D("2.333333")  # 14 / 6
    assert m.expectancy_per_trade == D("2")
    # 2/4 × 7 + 1/4 × (-6): el equilibrio aporta 0
    assert m.expectancy_pct == D("2")
    assert m.average_r_multiple == D("0.25")


def test_sin_perdidas_el_profit_factor_es_null_no_un_numero_inventado():
    m = _perf([_trade("5"), _trade("3")])
    assert m.profit_factor is None and m.losing_trades == 0 and m.average_loser_pct is None


def test_expectancy_pct_es_la_formula_probabilistica_del_plan():
    m = _perf([_trade("12"), _trade("12"), _trade("-8")])
    wr = D(2) / 3
    esperado = wr * D(12) + (1 - wr) * D(-8)
    assert abs(m.expectancy_pct - esperado) < D("0.000001")
    assert m.expectancy_per_trade == D("5.33333333")


def test_cero_operaciones_devuelve_ceros_y_nulls():
    m = _perf([])
    assert m.total_trades == 0 and m.net_pnl == 0
    assert m.win_rate_pct is None and m.expectancy_per_trade is None and m.profit_factor is None
    assert m.median_trade_return_pct is None and m.total_return_pct is None
    assert m.score_discrimination is None and m.max_drawdown_pct is None


def test_mae_mfe_y_scores_promedian_solo_los_que_existen():
    m = _perf([_trade("1", mae="-4", mfe="6", score="60"), _trade("1", mae="-2", mfe=None, score=None)])
    assert m.average_mae_pct == D("-3") and m.average_mfe_pct == D("6")
    assert m.average_opportunity_score == D("60")
    assert m.average_risk_score is None and m.average_confidence_score is None


def test_slippage_null_cuenta_como_cero_en_el_total():
    assert _perf([_trade("1")]).total_slippage == 0


def test_periodo_invalido_y_capital_invalido_se_rechazan():
    with pytest.raises(ValueError):
        calculate_performance([], [], None, scope_type="GLOBAL", scope_key=None, period_start=T0,
                              period_end=T0 - timedelta(days=1), initial_capital=D(1000))
    with pytest.raises(ValueError):
        calculate_performance([], [], None, scope_type="GLOBAL", scope_key=None, period_start=T0,
                              period_end=T0, initial_capital=D(0))


def test_el_orden_de_entrada_de_las_operaciones_no_cambia_el_resultado():
    trades = [_trade(str(p), dia=i, score=str(50 + i)) for i, p in enumerate([5, -3, 8, -1, 2, 7])]
    assert _perf(trades) == _perf(list(reversed(trades)))


# --- terciles del score dentro de las métricas ---


def test_terciles_alto_y_bajo_en_las_metricas():
    # 6 operaciones: score alto (90,80) ganan; bajo (30,20) pierden.
    trades = [
        _trade("10", score="90", dia=0), _trade("6", score="80", dia=1),
        _trade("1", score="60", dia=2), _trade("-1", score="50", dia=3),
        _trade("-4", score="30", dia=4), _trade("-8", score="20", dia=5),
    ]
    m = _perf(trades)
    assert m.high_score_expectancy == D("8") and m.low_score_expectancy == D("-6")
    assert m.score_discrimination == D("14")
    assert m.high_score_win_rate_pct == D("100") and m.low_score_win_rate_pct == D("0")


def test_con_menos_de_tres_operaciones_no_hay_discriminacion():
    m = _perf([_trade("1"), _trade("2")])
    assert m.score_discrimination is None and m.high_score_expectancy is None


# --- drawdown máximo sobre la curva de equity ---


def _curva(*valores):
    return [PuntoDeSerie(T0 + timedelta(minutes=i), D(v)) for i, v in enumerate(valores)]


def test_max_drawdown_es_propiedad_de_la_trayectoria_no_de_la_peor_operacion():
    dd = calculate_max_drawdown(_curva("1000", "1100", "1050", "990", "1200", "1080"))
    # pico 1100 → 990 = -10%; después pico 1200 → 1080 = -10% (empate: el primero)
    assert dd.pct == D("-10") and dd.value == D("-110")


def test_max_drawdown_cero_si_la_curva_solo_sube_y_none_si_esta_vacia():
    assert calculate_max_drawdown(_curva("1000", "1010", "1020")).pct == D("0")
    assert calculate_max_drawdown([]) is None


def test_max_drawdown_ordena_por_tiempo_sin_depender_del_orden_de_entrada():
    curva = _curva("1000", "1200", "900")
    assert calculate_max_drawdown(list(reversed(curva))) == calculate_max_drawdown(curva)
    assert calculate_max_drawdown(curva).pct == D("-25")


def test_max_drawdown_rechaza_equity_no_positivo():
    with pytest.raises(ValueError):
        calculate_max_drawdown(_curva("1000", "0"))


def test_performance_usa_la_curva_para_el_drawdown():
    m = _perf([_trade("5")], curve=_curva("1000", "1200", "1020"))
    assert m.max_drawdown_pct == D("-15") and m.max_drawdown_value == D("-180")


# --- benchmark ---


def test_retorno_y_promedio_simple():
    assert calculate_return_pct(D("100"), D("110")) == D("10")
    assert calculate_return_pct(D("100"), D("92")) == D("-8")
    assert average_return_pct([D("10"), D("-4"), D("0")]) == D("2")
    with pytest.raises(ValueError):
        average_return_pct([])
    with pytest.raises(ValueError):
        calculate_return_pct(D(0), D(1))


def test_benchmark_de_un_periodo_retorno_y_drawdown():
    b = calculate_benchmark(_curva("100", "120", "90", "110"))
    assert b.return_pct == D("10") and b.max_drawdown_pct == D("-25")


def test_benchmark_con_menos_de_dos_puntos_no_existe():
    assert calculate_benchmark(_curva("100")) == BenchmarkPeriodo()
    assert calculate_benchmark([]) == BenchmarkPeriodo()


def test_el_benchmark_se_copia_a_las_metricas():
    m = _perf([_trade("1")], bench=BenchmarkPeriodo(D("3.5"), D("-7")))
    assert m.benchmark_return_pct == D("3.5") and m.benchmark_max_drawdown_pct == D("-7")
    assert _perf([_trade("1")]).benchmark_return_pct is None


def test_los_resultados_son_inmutables():
    m = _perf([_trade("1")])
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.net_pnl = D(5)


# --- benchmark de una operación con precios observados ---


def test_benchmark_de_operacion_btc_y_promedio_simple_del_universo():
    from app.domain.rules.results import calculate_trade_benchmark

    universo = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    entrada = {"BTCUSDT": D("100"), "ETHUSDT": D("50"), "SOLUSDT": D("10")}
    salida = {"BTCUSDT": D("110"), "ETHUSDT": D("45"), "SOLUSDT": D("10")}
    b = calculate_trade_benchmark(entrada, salida, universe=universo)
    assert b.btc_return_pct == D("10")
    assert b.market_return_pct == D("0")  # (10 - 10 + 0) / 3


def test_si_falta_un_precio_el_mercado_es_none_pero_btc_puede_existir():
    from app.domain.rules.results import calculate_trade_benchmark

    universo = ("BTCUSDT", "ETHUSDT")
    entrada = {"BTCUSDT": D("100"), "ETHUSDT": D("50")}
    b = calculate_trade_benchmark(entrada, {"BTCUSDT": D("101")}, universe=universo)
    assert b.btc_return_pct == D("1") and b.market_return_pct is None
    b = calculate_trade_benchmark({"ETHUSDT": D("50")}, {"BTCUSDT": D("101"), "ETHUSDT": D("51")}, universe=universo)
    assert b.btc_return_pct is None and b.market_return_pct is None
    assert calculate_trade_benchmark({}, {}, universe=universo) == calculate_trade_benchmark({}, {}, universe=())


# --- métricas de un snapshot ---


def test_metricas_de_snapshot_antes_del_tp():
    from app.domain.rules.results import calculate_snapshot_metrics

    m = calculate_snapshot_metrics(
        entry_price=D("100"), market_price=D("104"), quantity=D("2"), stop_price=D("92"),
        target_price=D("112"), min_price_seen=D("97"), max_price_seen=D("106"),
    )
    assert m.position_value == D("208") and m.unrealized_pnl == D("8") and m.unrealized_return_pct == D("4")
    assert m.distance_to_stop_pct == D("11.538462")  # (104-92)/104
    assert m.distance_to_target_pct == D("7.692308")  # (112-104)/104
    assert m.mae_pct == D("-3") and m.mfe_pct == D("6")


def test_metricas_de_snapshot_primer_precio_y_extremos_nuevos():
    from app.domain.rules.results import calculate_snapshot_metrics

    m = calculate_snapshot_metrics(
        entry_price=D("100"), market_price=D("100"), quantity=D("1"), stop_price=D("92"),
        target_price=None, min_price_seen=None, max_price_seen=None,
    )
    assert m.mae_pct == D("0") and m.mfe_pct == D("0") and m.distance_to_target_pct is None
    n = calculate_snapshot_metrics(
        entry_price=D("100"), market_price=D("90"), quantity=D("1"), stop_price=None,
        target_price=None, min_price_seen=D("95"), max_price_seen=D("101"),
    )
    assert n.mae_pct == D("-10") and n.mfe_pct == D("1") and n.distance_to_stop_pct is None
    assert n.unrealized_pnl == D("-10")


def test_metricas_de_snapshot_rechazan_valores_no_positivos():
    from app.domain.rules.results import calculate_snapshot_metrics

    with pytest.raises(ValueError):
        calculate_snapshot_metrics(entry_price=D("0"), market_price=D("1"), quantity=D("1"), stop_price=None,
                                   target_price=None, min_price_seen=None, max_price_seen=None)


def test_serializar_y_leer_precios_no_pasa_por_float():
    from app.domain.rules.results import parse_prices, serialize_prices

    precios = {"BTCUSDT": D("67000.123456789012"), "ETHUSDT": D("0.1")}
    assert serialize_prices(precios) == {"BTCUSDT": "67000.123456789012", "ETHUSDT": "0.1"}
    assert parse_prices(serialize_prices(precios)) == precios
    assert parse_prices(None) == {}
