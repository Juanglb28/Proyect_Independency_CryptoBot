"""Equity del sistema (decisión del 2026-10-05): capital + P&L neto realizado
(comisiones incluidas) + P&L no realizado al precio observado. Y la regla de
la pausa en -15% sobre ese equity, en los umbrales exactos."""

from decimal import Decimal

import pytest

from app.domain.enums import LadoOrden, TierEntrada
from app.domain.rules.risk import (
    MotivoRechazoEntrada,
    OrdenEjecutada,
    PosicionMarcada,
    SituacionDeCupos,
    calculate_equity,
    is_drawdown_limit_reached,
    validate_entry,
)

D = Decimal
CAPITAL = D("1000")


def _compra(qty, price, fee):
    return OrdenEjecutada(LadoOrden.BUY, D(qty), D(price), D(fee))


def _venta(qty, price, fee):
    return OrdenEjecutada(LadoOrden.SELL, D(qty), D(price), D(fee))


def test_sin_operaciones_el_equity_es_el_capital():
    assert calculate_equity(initial_capital=CAPITAL, filled_orders=[], open_positions=[]) == D("1000")


def test_posicion_abierta_sin_movimiento_solo_pierde_la_comision_de_entrada():
    # 100 USD a 50: 2 unidades, comisión 0,1 USD. Precio sin cambio.
    equity = calculate_equity(
        initial_capital=CAPITAL,
        filled_orders=[_compra("2", "50", "0.1")],
        open_positions=[PosicionMarcada(D("2"), D("50"))],
    )
    assert equity == D("999.9")


def test_pnl_no_realizado_suma_y_resta_al_precio_observado():
    ordenes = [_compra("2", "50", "0.1")]
    sube = calculate_equity(
        initial_capital=CAPITAL, filled_orders=ordenes, open_positions=[PosicionMarcada(D("2"), D("55"))]
    )
    baja = calculate_equity(
        initial_capital=CAPITAL, filled_orders=ordenes, open_positions=[PosicionMarcada(D("2"), D("46"))]
    )
    assert sube == D("1009.9")  # +10 USD no realizados, -0,1 de comisión
    assert baja == D("991.9")  # -8 USD no realizados, -0,1 de comisión


def test_posicion_cerrada_con_ganancia_incluye_ambas_comisiones():
    # Compra 2 a 50 (fee 0,1) y venta de las 2 a 56 (fee 0,112): +12 - 0,212
    equity = calculate_equity(
        initial_capital=CAPITAL,
        filled_orders=[_compra("2", "50", "0.1"), _venta("2", "56", "0.112")],
        open_positions=[],
    )
    assert equity == D("1011.788")


def test_cierre_parcial_mezcla_realizado_y_no_realizado():
    # Compra 2 a 50; vende 1 a 56 (TP parcial); queda 1 valorada a 58.
    equity = calculate_equity(
        initial_capital=CAPITAL,
        filled_orders=[_compra("2", "50", "0.1"), _venta("1", "56", "0.056")],
        open_positions=[PosicionMarcada(D("1"), D("58"))],
    )
    # 1000 - 100,1 + 55,944 + 58 = 1013,844
    assert equity == D("1013.844")


def test_varias_posiciones_abiertas_se_suman():
    equity = calculate_equity(
        initial_capital=CAPITAL,
        filled_orders=[_compra("2", "50", "0.1"), _compra("10", "10", "0.1")],
        open_positions=[PosicionMarcada(D("2"), D("50")), PosicionMarcada(D("10"), D("9"))],
    )
    # 1000 - 100,1 - 100,1 + 100 + 90 = 989,8
    assert equity == D("989.8")


def test_el_resultado_se_redondea_a_8_decimales():
    equity = calculate_equity(
        initial_capital=CAPITAL,
        filled_orders=[_compra("3", "0.333333333333", "0.1")],
        open_positions=[PosicionMarcada(D("3"), D("0.333333333333"))],
    )
    assert equity == equity.quantize(D("1E-8"))
    assert equity == D("999.90000000")


def test_capital_no_positivo_se_rechaza():
    with pytest.raises(ValueError):
        calculate_equity(initial_capital=D("0"), filled_orders=[], open_positions=[])


# --- Pausa por drawdown, umbrales exactos sobre el equity -------------------


def test_drawdown_exactamente_en_menos_15_por_ciento_pausa():
    assert is_drawdown_limit_reached(D("850"), D("1000")) is True


def test_drawdown_un_centavo_antes_de_menos_15_no_pausa():
    assert is_drawdown_limit_reached(D("850.01"), D("1000")) is False


def test_drawdown_se_mide_contra_el_pico_no_contra_el_capital():
    # Pico 1.200: el umbral es 1.020, no 850.
    assert is_drawdown_limit_reached(D("1020"), D("1200")) is True
    assert is_drawdown_limit_reached(D("1020.01"), D("1200")) is False


# --- Parada de emergencia (archivo PARAR) en la validación de entradas ------


def _cupos(**kw):
    base = dict(
        posiciones_abiertas=0,
        entradas_hoy=0,
        mayor_riesgo_abiertas=0,
        simbolos_abiertos=frozenset(),
        sistema_pausado=False,
    )
    base.update(kw)
    return SituacionDeCupos(**base)


def test_parada_de_emergencia_bloquea_toda_entrada_nueva_con_su_motivo():
    v = validate_entry(symbol="BTCUSDT", tier=TierEntrada.NORMAL, cupos=_cupos(parada_emergencia=True))
    assert v.permitida is False
    assert v.motivos_rechazo == (MotivoRechazoEntrada.PARADA_DE_EMERGENCIA,)


def test_sin_parada_de_emergencia_la_entrada_sigue_permitida():
    v = validate_entry(symbol="BTCUSDT", tier=TierEntrada.NORMAL, cupos=_cupos())
    assert v.permitida is True


def test_pausa_y_parada_juntas_reportan_ambos_motivos():
    v = validate_entry(
        symbol="BTCUSDT",
        tier=TierEntrada.NORMAL,
        cupos=_cupos(sistema_pausado=True, parada_emergencia=True),
    )
    assert set(v.motivos_rechazo) == {
        MotivoRechazoEntrada.SISTEMA_PAUSADO,
        MotivoRechazoEntrada.PARADA_DE_EMERGENCIA,
    }
