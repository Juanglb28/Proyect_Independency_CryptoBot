"""Definiciones de soporte de H1 (app.domain.rules.support)."""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from app.domain.rules.support import (
    TOLERANCIA_CERCANIA,
    esta_cerca,
    indices_minimos_oscilacion,
    soporte_fractal,
    soporte_fractal_desde_indices,
    soporte_minimo_90d,
)


def D(*valores) -> list[Decimal]:
    return [Decimal(str(v)) for v in valores]


def test_parametros_del_protocolo_estan_fijados():
    # Pre-registrados en claude/protocolo-h1-soporte.md: cambiarlos = hipótesis nueva.
    from app.domain.rules import support

    assert support.FRACTAL_N == 5
    assert support.VENTANA_FRACTAL_VELAS == 180
    assert support.VENTANA_MINIMO_VELAS == 90
    assert support.TOLERANCIA_CERCANIA == Decimal("0.02")
    assert support.UMBRAL_RSI_4H == Decimal("35")
    assert support.VELAS_DIARIAS_MINIMAS == 190
    assert support.VELAS_4H_MINIMAS == 200


def test_ejemplo_del_protocolo_el_dia_6_es_minimo_de_oscilacion():
    lows = D(83200, 82600, 81900, 81100, 80700, 80347, 80900, 81600, 82300, 82800, 83100)
    assert indices_minimos_oscilacion(lows, 5) == [5]


def test_un_empate_con_una_vecina_no_cuenta():
    lows = D(10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10)
    assert indices_minimos_oscilacion(lows, 5) == [5]
    empatado = D(10, 9, 8, 7, 5, 5, 6, 7, 8, 9, 10)
    assert indices_minimos_oscilacion(empatado, 5) == []


def test_un_minimo_sin_n_velas_posteriores_no_esta_confirmado():
    # El mínimo más bajo está en las últimas 3 velas: aún no hay 5 posteriores.
    lows = D(10, 10, 10, 10, 10, 10, 10, 9, 8, 7)
    assert soporte_fractal(lows, Decimal(20)) is None


def test_soporte_fractal_elige_el_de_mayor_nivel_por_debajo_del_precio():
    # Dos mínimos de oscilación (índices 5 y 17, niveles 50 y 70) y un precio de 75.
    lows = D(*([100] * 5), 50, *([100] * 5), *([95] * 6), 70, *([100] * 5))
    assert indices_minimos_oscilacion(lows, 5) == [5, 17]
    assert soporte_fractal(lows, Decimal(75)) == Decimal(70)
    assert soporte_fractal(lows, Decimal(60)) == Decimal(50)  # el de 70 quedó por encima del precio
    assert soporte_fractal(lows, Decimal(40)) is None  # el precio rompió todos los soportes


def test_soporte_fractal_respeta_la_ventana_de_180_velas():
    lows = D(*([100] * 5), 50, *([100] * 5), *([100] * 200))
    assert soporte_fractal(lows, Decimal(60), ventana=180) is None
    assert soporte_fractal(lows, Decimal(60), ventana=400) == Decimal(50)


def test_ausencia_de_look_ahead_cada_prefijo_da_lo_mismo_con_indices_de_la_serie_completa():
    rng = random.Random(7)
    lows = [Decimal(rng.randint(50, 150)) for _ in range(300)]
    indices_completos = indices_minimos_oscilacion(lows)
    precio = Decimal(120)
    for largo in range(12, len(lows) + 1):
        truncado = soporte_fractal(lows[:largo], precio)
        desde_completa = soporte_fractal_desde_indices(lows, indices_completos, largo - 1, precio)
        assert truncado == desde_completa, f"diferencia con {largo} velas"


def test_soporte_minimo_90d():
    lows = D(*range(200, 100, -1))  # 100 velas, la mínima es la última (101)
    assert soporte_minimo_90d(lows) == Decimal(101)
    assert soporte_minimo_90d(lows[:89]) is None  # historia insuficiente: dato faltante, no un supuesto
    assert soporte_minimo_90d(D(*([50] + [100] * 95))) == Decimal(100)  # el 50 quedó fuera de las últimas 90


@pytest.mark.parametrize(
    ("precio", "esperado"),
    [("100", True), ("102", True), ("102.01", False), ("99.99", False)],
)
def test_esta_cerca_bordes_exactos(precio, esperado):
    assert esta_cerca(Decimal(precio), Decimal(100), TOLERANCIA_CERCANIA) is esperado


def test_sin_soporte_no_esta_cerca():
    assert esta_cerca(Decimal(100), None) is False
