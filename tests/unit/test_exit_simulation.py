"""Simulación secundaria de la salida real (app.data.exit_simulation)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.data.exchange_client import Candle
from app.data.exit_simulation import simular_salida
from app.domain.rules.risk import (
    COMISION_POR_LADO,
    TAKE_PROFIT_FRACCION_A_CERRAR,
    calculate_initial_levels,
    calculate_trailing_stop,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
UNO = Decimal(1)


def _v(i: int, o, h, low, c) -> Candle:
    return Candle(
        symbol="X", timeframe="1h", open_time=T0 + timedelta(hours=i),
        open=Decimal(str(o)), high=Decimal(str(h)), low=Decimal(str(low)), close=Decimal(str(c)),
        volume=Decimal(1),
    )


ENTRADA = Decimal(100)
NIV = calculate_initial_levels(ENTRADA)


def test_stop_pierde_el_8_por_ciento_mas_comisiones():
    velas = [_v(0, 100, 100, 100, 100), _v(1, 100, 101, 91, 92)]
    r = simular_salida(velas, 0)
    assert r is not None and r.motivo == "STOP_LOSS" and r.velas_hasta_cierre == 1
    esperado = (NIV.stop_price / ENTRADA) * (UNO - COMISION_POR_LADO) - UNO - COMISION_POR_LADO
    assert r.retorno_neto == esperado
    assert r.retorno_neto < Decimal("-0.08")


def test_misma_vela_stop_y_objetivo_cuenta_como_stop():
    velas = [_v(0, 100, 100, 100, 100), _v(1, 100, 113, 91, 100)]
    r = simular_salida(velas, 0)
    assert r is not None and r.motivo == "STOP_LOSS"


def test_objetivo_parcial_y_trailing_sobre_el_resto():
    velas = [
        _v(0, 100, 100, 100, 100),
        _v(1, 100, 113, 99, 112),   # toca +12 %: cierra 50 %; el resto de la vela no se evalúa
        _v(2, 112, 120, 119, 119),  # máximo 120; trailing 112,8; low 119 > trailing
        _v(3, 119, 118, 112, 113),  # low 112 <= trailing(120) -> sale
    ]
    r = simular_salida(velas, 0)
    assert r is not None and r.motivo == "TRAILING_STOP" and r.velas_hasta_cierre == 3
    cantidad = UNO / ENTRADA
    mitad = cantidad * TAKE_PROFIT_FRACCION_A_CERRAR
    trailing = calculate_trailing_stop(Decimal(120))
    cobrado = mitad * NIV.target_price * (UNO - COMISION_POR_LADO) + mitad * trailing * (UNO - COMISION_POR_LADO)
    assert r.retorno_neto == cobrado - UNO - COMISION_POR_LADO
    assert r.retorno_neto > 0


def test_tras_el_parcial_el_maximo_arranca_en_el_objetivo_aunque_la_vela_siga_subiendo():
    # En la vela del parcial el high llega a 130, pero el pico NO se actualiza hasta la vela siguiente.
    velas = [
        _v(0, 100, 100, 100, 100),
        _v(1, 100, 130, 99, 125),
        _v(2, 125, 125, 105, 106),  # pico = max(112, 125) = 125 -> trailing 117,5; low 105 -> sale a 117,5
    ]
    r = simular_salida(velas, 0)
    assert r is not None and r.velas_hasta_cierre == 2
    trailing = calculate_trailing_stop(Decimal(125))
    cantidad = UNO / ENTRADA
    mitad = cantidad * TAKE_PROFIT_FRACCION_A_CERRAR
    cobrado = mitad * NIV.target_price * (UNO - COMISION_POR_LADO) + mitad * trailing * (UNO - COMISION_POR_LADO)
    assert r.retorno_neto == cobrado - UNO - COMISION_POR_LADO


def test_censurada_si_nunca_cierra_del_todo():
    sin_toques = [_v(0, 100, 100, 100, 100), _v(1, 100, 105, 95, 100)]
    assert simular_salida(sin_toques, 0) is None
    parcial_sin_trailing = [_v(0, 100, 100, 100, 100), _v(1, 100, 113, 99, 112), _v(2, 112, 130, 125, 129)]
    assert simular_salida(parcial_sin_trailing, 0) is None


def test_sin_velas_posteriores_devuelve_none():
    assert simular_salida([_v(0, 100, 100, 100, 100)], 0) is None
