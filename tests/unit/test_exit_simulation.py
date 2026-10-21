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


# --------------------------------------------------- marcado a mercado (diagnóstico de censura)
from app.data.exit_simulation import retorno_marcado_a_mercado  # noqa: E402


def test_marcado_de_una_posicion_cerrada_es_el_resultado_real():
    velas = [_v(0, 100, 100, 100, 100), _v(1, 100, 101, 91, 92)]
    assert retorno_marcado_a_mercado(velas, 0) == simular_salida(velas, 0).retorno_neto


def test_marcado_de_una_posicion_abierta_sin_parcial_valora_todo_al_ultimo_cierre():
    velas = [_v(0, 100, 100, 100, 100), _v(1, 100, 104, 98, 103)]
    assert simular_salida(velas, 0) is None
    esperado = (UNO / ENTRADA) * Decimal(103) * (UNO - COMISION_POR_LADO) - UNO - COMISION_POR_LADO
    assert retorno_marcado_a_mercado(velas, 0) == esperado


def test_marcado_tras_cierre_parcial_suma_lo_cobrado_y_valora_lo_que_queda():
    objetivo = NIV.target_price
    velas = [_v(0, 100, 100, 100, 100), _v(1, 100, objetivo + 1, 99, objetivo), _v(2, objetivo, objetivo + 1, objetivo - 1, objetivo + Decimal("0.5"))]
    assert simular_salida(velas, 0) is None  # trailing 6 % bajo el pico no se tocó
    cantidad = UNO / ENTRADA
    vendida = cantidad * TAKE_PROFIT_FRACCION_A_CERRAR
    cobrado = vendida * objetivo * (UNO - COMISION_POR_LADO)
    resto = (cantidad - vendida) * (objetivo + Decimal("0.5")) * (UNO - COMISION_POR_LADO)
    assert retorno_marcado_a_mercado(velas, 0) == cobrado + resto - UNO - COMISION_POR_LADO


def test_marcado_sin_velas_posteriores_es_none():
    assert retorno_marcado_a_mercado([_v(0, 100, 100, 100, 100)], 0) is None


# --------------------------------------- fricción de ejecución (prueba de estrés, 2026-10-21)
# Protocolo: claude/protocolo-friccion.md. Sin fricción NADA cambia (resultados publicados).
import pytest  # noqa: E402

from app.data.exit_simulation import MAXIMO_PRIMERO, MINIMO_PRIMERO, SIN_FRICCION, Friccion  # noqa: E402

UNO_POR_CIENTO = Decimal("0.01")
_PARCIAL = _v(1, 100, 113, 99, 112)  # toca el objetivo (112): cierra el 50 %; pico = 112


def _series_de_control() -> list[list[Candle]]:
    return [
        [_v(0, 100, 100, 100, 100), _v(1, 100, 101, 91, 92)],  # stop
        [_v(0, 100, 100, 100, 100), _v(1, 100, 113, 91, 100)],  # stop y objetivo
        [_v(0, 100, 100, 100, 100), _PARCIAL, _v(2, 112, 120, 119, 119), _v(3, 119, 118, 112, 113)],  # parcial + trailing
        [_v(0, 100, 100, 100, 100), _v(1, 100, 104, 98, 103)],  # censurada sin parcial
        [_v(0, 100, 100, 100, 100), _PARCIAL, _v(2, 112, 130, 125, 129)],  # censurada tras parcial
    ]


def test_sin_friccion_es_identico_al_comportamiento_de_siempre():
    """Pasar SIN_FRICCION explícito == no pasar nada, en cierres y en valoración a mercado."""
    for velas in _series_de_control():
        for orden in (MAXIMO_PRIMERO, MINIMO_PRIMERO):
            base = simular_salida(velas, 0, orden_intravela=orden)
            explicita = simular_salida(velas, 0, orden_intravela=orden, friccion=SIN_FRICCION)
            assert base == explicita
            assert retorno_marcado_a_mercado(velas, 0, orden_intravela=orden) == retorno_marcado_a_mercado(
                velas, 0, orden_intravela=orden, friccion=Friccion()
            )


def test_friccion_nula_y_no_nula():
    assert SIN_FRICCION.es_nula and Friccion().es_nula
    assert not Friccion(slippage_por_lado=Decimal("0.001")).es_nula
    assert not Friccion(extra_slippage_stop=Decimal("0.005")).es_nula
    assert not Friccion(huecos=True).es_nula


@pytest.mark.parametrize(
    "kwargs",
    [
        {"slippage_por_lado": Decimal("-0.001")},
        {"extra_slippage_stop": Decimal("-0.001")},
        {"slippage_por_lado": 0.001},  # float no permitido para dinero
        {"slippage_por_lado": Decimal("0.6"), "extra_slippage_stop": Decimal("0.4")},
    ],
)
def test_friccion_rechaza_valores_invalidos(kwargs):
    with pytest.raises(ValueError):
        Friccion(**kwargs)


def test_slippage_de_entrada_mueve_los_niveles_y_se_paga_en_la_salida_por_stop():
    s = UNO_POR_CIENTO
    entrada = ENTRADA * (UNO + s)  # 101
    niveles = calculate_initial_levels(entrada)
    velas = [_v(0, 100, 100, 100, 100), _v(1, 100, 101, 80, 85)]
    r = simular_salida(velas, 0, friccion=Friccion(slippage_por_lado=s))
    assert r is not None and r.motivo == "STOP_LOSS"
    esperado = (UNO / entrada) * niveles.stop_price * (UNO - s) * (UNO - COMISION_POR_LADO) - UNO - COMISION_POR_LADO
    assert r.retorno_neto == esperado
    assert r.retorno_neto < simular_salida(velas, 0).retorno_neto


def test_extra_slippage_solo_castiga_al_stop_no_al_trailing():
    extra = Decimal("0.005")
    velas_stop = [_v(0, 100, 100, 100, 100), _v(1, 100, 101, 91, 92)]
    con = simular_salida(velas_stop, 0, friccion=Friccion(extra_slippage_stop=extra))
    esperado = (UNO / ENTRADA) * NIV.stop_price * (UNO - extra) * (UNO - COMISION_POR_LADO) - UNO - COMISION_POR_LADO
    assert con.retorno_neto == esperado

    velas_trailing = [_v(0, 100, 100, 100, 100), _PARCIAL, _v(2, 112, 120, 119, 119), _v(3, 119, 118, 112, 113)]
    assert simular_salida(velas_trailing, 0, friccion=Friccion(extra_slippage_stop=extra)) == simular_salida(
        velas_trailing, 0
    )


def test_slippage_se_paga_tambien_en_objetivo_y_trailing():
    s = Decimal("0.0025")
    velas = [_v(0, 100, 100, 100, 100), _PARCIAL, _v(2, 112, 120, 119, 119), _v(3, 119, 118, 112, 113)]
    entrada = ENTRADA * (UNO + s)
    niveles = calculate_initial_levels(entrada)
    # El objetivo/stop se recalculan sobre 100,25; con ellos el parcial (112,28) se alcanza igual en la vela 1.
    assert niveles.target_price > Decimal(112)
    # Con s = 0,25 % el objetivo sube a ~112,28 y la vela 1 (high 113) sigue tocándolo.
    r = simular_salida(velas, 0, friccion=Friccion(slippage_por_lado=s))
    cantidad = UNO / entrada
    mitad = cantidad * TAKE_PROFIT_FRACCION_A_CERRAR
    pico_pre = niveles.target_price
    # tras el parcial el pico arranca en el objetivo; vela 2 sube el pico a 120; vela 3 toca el trailing de 120
    trailing = calculate_trailing_stop(Decimal(120))
    cobrado = (
        mitad * niveles.target_price * (UNO - s) * (UNO - COMISION_POR_LADO)
        + mitad * trailing * (UNO - s) * (UNO - COMISION_POR_LADO)
    )
    assert pico_pre < Decimal(120)
    assert r.motivo == "TRAILING_STOP"
    assert r.retorno_neto == cobrado - UNO - COMISION_POR_LADO


def test_hueco_en_el_stop_ejecuta_a_la_apertura_solo_si_huecos_esta_activo():
    velas = [_v(0, 100, 100, 100, 100), _v(1, 90, 91, 89, 90)]  # abre en 90, bajo el stop (92)
    sin = simular_salida(velas, 0)
    con = simular_salida(velas, 0, friccion=Friccion(huecos=True))
    assert sin.retorno_neto == (NIV.stop_price / ENTRADA) * (UNO - COMISION_POR_LADO) - UNO - COMISION_POR_LADO
    assert con.retorno_neto == (Decimal(90) / ENTRADA) * (UNO - COMISION_POR_LADO) - UNO - COMISION_POR_LADO
    assert con.retorno_neto < sin.retorno_neto


def test_vela_que_abre_sobre_el_stop_no_recibe_hueco():
    velas = [_v(0, 100, 100, 100, 100), _v(1, 100, 101, 91, 92)]  # abre en 100 (> stop) y cae hasta 91
    assert simular_salida(velas, 0, friccion=Friccion(huecos=True)) == simular_salida(velas, 0)


def test_no_hay_huecos_favorables_en_el_objetivo():
    velas = [_v(0, 100, 100, 100, 100), _v(1, 125, 130, 120, 128)]  # abre muy por encima del objetivo
    assert simular_salida(velas, 0, friccion=Friccion(huecos=True)) == simular_salida(velas, 0)
    assert retorno_marcado_a_mercado(velas, 0, friccion=Friccion(huecos=True)) == retorno_marcado_a_mercado(velas, 0)
    # Valor ABSOLUTO: el parcial se cobra en el nivel del objetivo (112), no en la apertura (125).
    mitad = (UNO / ENTRADA) * TAKE_PROFIT_FRACCION_A_CERRAR
    esperado = (
        mitad * NIV.target_price * (UNO - COMISION_POR_LADO)
        + mitad * Decimal(128) * (UNO - COMISION_POR_LADO)
        - UNO - COMISION_POR_LADO
    )
    assert retorno_marcado_a_mercado(velas, 0, friccion=Friccion(huecos=True)) == esperado


@pytest.mark.parametrize("orden", [MAXIMO_PRIMERO, MINIMO_PRIMERO])
def test_hueco_en_el_trailing_vigente_ejecuta_a_la_apertura(orden):
    trailing_vigente = calculate_trailing_stop(NIV.target_price)  # el pico arranca en el objetivo
    velas = [_v(0, 100, 100, 100, 100), _PARCIAL, _v(2, 100, 101, 99, 100)]  # abre en 100 < trailing vigente
    assert Decimal(100) < trailing_vigente
    sin = simular_salida(velas, 0, orden_intravela=orden)
    con = simular_salida(velas, 0, orden_intravela=orden, friccion=Friccion(huecos=True))
    cantidad = UNO / ENTRADA
    mitad = cantidad * TAKE_PROFIT_FRACCION_A_CERRAR
    parcial = mitad * NIV.target_price * (UNO - COMISION_POR_LADO)
    assert sin is not None and con is not None
    assert sin.retorno_neto == parcial + mitad * trailing_vigente * (UNO - COMISION_POR_LADO) - UNO - COMISION_POR_LADO
    assert con.retorno_neto == parcial + mitad * Decimal(100) * (UNO - COMISION_POR_LADO) - UNO - COMISION_POR_LADO


def test_vela_que_abre_sobre_el_trailing_no_recibe_hueco_aunque_luego_caiga():
    velas = [_v(0, 100, 100, 100, 100), _PARCIAL, _v(2, 110, 120, 105, 106)]  # abre 110 > trailing vigente (105,28)
    assert simular_salida(velas, 0, friccion=Friccion(huecos=True)) == simular_salida(velas, 0)


def test_valoracion_a_mercado_descuenta_el_slippage_de_salida():
    s = UNO_POR_CIENTO
    velas = [_v(0, 100, 100, 100, 100), _v(1, 100, 104, 98, 103)]
    entrada = ENTRADA * (UNO + s)
    friccion = Friccion(slippage_por_lado=s)
    assert simular_salida(velas, 0, friccion=friccion) is None
    esperado = (UNO / entrada) * Decimal(103) * (UNO - s) * (UNO - COMISION_POR_LADO) - UNO - COMISION_POR_LADO
    assert retorno_marcado_a_mercado(velas, 0, friccion=friccion) == esperado


def test_mas_friccion_nunca_mejora_el_retorno():
    escalones = [
        SIN_FRICCION,
        Friccion(slippage_por_lado=Decimal("0.001")),
        Friccion(slippage_por_lado=Decimal("0.0025")),
        Friccion(slippage_por_lado=Decimal("0.005")),
    ]
    for velas in _series_de_control():
        retornos = [retorno_marcado_a_mercado(velas, 0, friccion=f) for f in escalones]
        assert all(a >= b for a, b in zip(retornos, retornos[1:])), retornos
