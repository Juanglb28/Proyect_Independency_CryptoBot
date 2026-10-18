"""Orden intravela de la simulación de salida (estudio de sensibilidad, 2026-10-17).

El valor por defecto («máximo primero») es el de todos los resultados publicados y
no debe cambiar. «Mínimo primero» es la lectura inversa: ninguna de las dos es
siempre la pesimista, y estos tests fijan exactamente en qué casos difieren.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.data.exchange_client import Candle
from app.data.exit_simulation import (
    MAXIMO_PRIMERO,
    MINIMO_PRIMERO,
    retorno_marcado_a_mercado,
    simular_salida,
)
from app.domain.rules.risk import (
    COMISION_POR_LADO,
    TAKE_PROFIT_FRACCION_A_CERRAR,
    calculate_initial_levels,
    calculate_trailing_stop,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
UNO = Decimal(1)
ENTRADA = Decimal(100)
NIV = calculate_initial_levels(ENTRADA)


def _v(i: int, o, h, low, c) -> Candle:
    return Candle(
        symbol="X", timeframe="1h", open_time=T0 + timedelta(hours=i),
        open=Decimal(str(o)), high=Decimal(str(h)), low=Decimal(str(low)), close=Decimal(str(c)),
        volume=Decimal(1),
    )


def _retorno_con_trailing(pico: int) -> Decimal:
    """Retorno esperado: parcial al objetivo y el resto vendido al trailing del `pico`."""
    mitad = (UNO / ENTRADA) * TAKE_PROFIT_FRACCION_A_CERRAR
    cobrado = mitad * NIV.target_price * (UNO - COMISION_POR_LADO)
    cobrado += mitad * calculate_trailing_stop(Decimal(pico)) * (UNO - COMISION_POR_LADO)
    return cobrado - UNO - COMISION_POR_LADO


ENTRADA_Y_PARCIAL = [_v(0, 100, 100, 100, 100), _v(1, 100, 113, 99, 112)]  # parcial: el pico arranca en 112


def test_el_valor_por_defecto_es_maximo_primero():
    velas = [*ENTRADA_Y_PARCIAL, _v(2, 112, 130, 110, 125), _v(3, 125, 126, 120, 121)]
    assert simular_salida(velas, 0) == simular_salida(velas, 0, orden_intravela=MAXIMO_PRIMERO)
    assert retorno_marcado_a_mercado(velas, 0) == retorno_marcado_a_mercado(velas, 0, orden_intravela=MAXIMO_PRIMERO)


def test_un_orden_desconocido_se_rechaza():
    velas = [*ENTRADA_Y_PARCIAL, _v(2, 112, 130, 110, 125)]
    with pytest.raises(ValueError, match="orden_intravela"):
        simular_salida(velas, 0, orden_intravela="otro")
    with pytest.raises(ValueError, match="orden_intravela"):
        retorno_marcado_a_mercado(velas, 0, orden_intravela="otro")


def test_antes_del_objetivo_las_dos_lecturas_son_iguales_el_stop_gana_siempre():
    velas = [_v(0, 100, 100, 100, 100), _v(1, 100, 113, 91, 100)]
    a = simular_salida(velas, 0, orden_intravela=MAXIMO_PRIMERO)
    b = simular_salida(velas, 0, orden_intravela=MINIMO_PRIMERO)
    assert a == b and a is not None and a.motivo == "STOP_LOSS"


def test_si_el_low_toca_el_trailing_viejo_la_lectura_inversa_sale_mas_bajo():
    # pico previo 112 -> trailing viejo 105,28. La vela sube a 130 (trailing nuevo 122,2) y baja a 100.
    velas = [*ENTRADA_Y_PARCIAL, _v(2, 112, 130, 100, 101)]
    base = simular_salida(velas, 0, orden_intravela=MAXIMO_PRIMERO)
    inversa = simular_salida(velas, 0, orden_intravela=MINIMO_PRIMERO)
    assert base is not None and inversa is not None
    assert base.velas_hasta_cierre == inversa.velas_hasta_cierre == 2
    assert base.retorno_neto == _retorno_con_trailing(130)
    assert inversa.retorno_neto == _retorno_con_trailing(112)
    assert inversa.retorno_neto < base.retorno_neto


def test_si_solo_toca_el_trailing_nuevo_la_lectura_inversa_sigue_y_puede_salir_mejor():
    # low 110 > trailing viejo 105,28 pero <= trailing nuevo 122,2: la base sale en esta vela; la inversa no.
    velas = [*ENTRADA_Y_PARCIAL, _v(2, 112, 130, 110, 125), _v(3, 125, 140, 125, 138), _v(4, 138, 139, 120, 121)]
    base = simular_salida(velas, 0, orden_intravela=MAXIMO_PRIMERO)
    inversa = simular_salida(velas, 0, orden_intravela=MINIMO_PRIMERO)
    assert base is not None and inversa is not None
    assert base.velas_hasta_cierre == 2 and base.retorno_neto == _retorno_con_trailing(130)
    # inversa: v3 sube el pico a 140 (low 125 <= trailing(130)=122,2? no, 125 > 122,2 -> sigue); v4 low 120 <= trailing(140)=131,6
    assert inversa.velas_hasta_cierre == 4 and inversa.retorno_neto == _retorno_con_trailing(140)
    assert inversa.retorno_neto > base.retorno_neto


def test_si_el_low_no_toca_ni_el_trailing_nuevo_las_dos_lecturas_coinciden():
    velas = [*ENTRADA_Y_PARCIAL, _v(2, 112, 130, 123, 128), _v(3, 128, 129, 100, 101)]
    assert simular_salida(velas, 0, orden_intravela=MAXIMO_PRIMERO) == simular_salida(
        velas, 0, orden_intravela=MINIMO_PRIMERO
    )


def test_sin_nuevo_maximo_las_dos_lecturas_coinciden():
    # el high no supera el pico previo (112): el trailing nuevo es el viejo
    velas = [*ENTRADA_Y_PARCIAL, _v(2, 112, 112, 100, 101)]
    a = simular_salida(velas, 0, orden_intravela=MAXIMO_PRIMERO)
    b = simular_salida(velas, 0, orden_intravela=MINIMO_PRIMERO)
    assert a == b and a is not None and a.retorno_neto == _retorno_con_trailing(112)


def test_la_lectura_inversa_tambien_se_aplica_al_retorno_marcado_a_mercado():
    # termina con la posición abierta en la lectura inversa y cerrada en la base
    velas = [*ENTRADA_Y_PARCIAL, _v(2, 112, 130, 110, 125)]
    assert simular_salida(velas, 0, orden_intravela=MAXIMO_PRIMERO) is not None
    assert simular_salida(velas, 0, orden_intravela=MINIMO_PRIMERO) is None  # censurada
    marcado = retorno_marcado_a_mercado(velas, 0, orden_intravela=MINIMO_PRIMERO)
    assert marcado is not None
