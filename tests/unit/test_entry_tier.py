"""Tests de app.domain.rules.entry_tier (segunda ronda de entradas de mayor
riesgo, decisión del usuario 2026-10-04). Cubren los BORDES EXACTOS de cada
umbral — la ruta que decide si algo se propone como entrada debe probarse en
el valor justo, no solo lejos de él."""

from decimal import Decimal

import pytest

from app.domain.enums import TierEntrada
from app.domain.rules.entry_tier import (
    clasificar_tier,
    en_zona_segunda_ronda,
    evaluar_segunda_ronda,
)


def _rsi(h1="55", h4="55", h12="55", d1="60"):
    return {"1h": Decimal(h1), "4h": Decimal(h4), "12h": Decimal(h12), "1d": Decimal(d1)}


# --- zona [50, 55) ------------------------------------------------------------


@pytest.mark.parametrize(
    "puntaje, esperado",
    [("49.99", False), ("50", True), ("52.5", True), ("54.99", True), ("55", False), ("80", False)],
)
def test_zona_segunda_ronda_bordes_exactos(puntaje, esperado):
    assert en_zona_segunda_ronda(Decimal(puntaje)) is esperado


# --- verificaciones -----------------------------------------------------------


def test_todo_cumple_es_elegible():
    r = evaluar_segunda_ronda(rsi_por_temporalidad=_rsi(), rsi_ratio_btc_diario=Decimal("60"))
    assert r.elegible is True
    assert r.faltantes == ()


def test_rsi_ratio_exactamente_50_no_cumple_es_estricto():
    r = evaluar_segunda_ronda(rsi_por_temporalidad=_rsi(), rsi_ratio_btc_diario=Decimal("50"))
    assert r.elegible is False
    assert r.criterios["fuerza_relativa_btc"]["cumple"] is False


def test_rsi_ratio_apenas_sobre_50_cumple():
    r = evaluar_segunda_ronda(rsi_por_temporalidad=_rsi(), rsi_ratio_btc_diario=Decimal("50.01"))
    assert r.elegible is True


def test_momentum_exige_3_de_4_temporalidades_exactas():
    tres = evaluar_segunda_ronda(
        rsi_por_temporalidad=_rsi(h1="49"), rsi_ratio_btc_diario=Decimal("60")
    )
    assert tres.elegible is True
    assert tres.criterios["momentum_multitemporal"]["temporalidades_con_rsi_sobre_umbral"] == ["4h", "12h", "1d"]

    dos = evaluar_segunda_ronda(
        rsi_por_temporalidad=_rsi(h1="49", h4="49"), rsi_ratio_btc_diario=Decimal("60")
    )
    assert dos.elegible is False
    assert dos.criterios["momentum_multitemporal"]["cumple"] is False


def test_rsi_exactamente_50_en_una_temporalidad_no_cuenta_como_momentum():
    r = evaluar_segunda_ronda(
        rsi_por_temporalidad=_rsi(h1="50", h4="50"), rsi_ratio_btc_diario=Decimal("60")
    )
    assert r.elegible is False  # solo 12h y 1d superan 50 estrictamente


def test_tope_sobrecompra_diario_70_exacto_cumple_y_70_01_bloquea():
    en_tope = evaluar_segunda_ronda(rsi_por_temporalidad=_rsi(d1="70"), rsi_ratio_btc_diario=Decimal("60"))
    assert en_tope.elegible is True

    sobre = evaluar_segunda_ronda(rsi_por_temporalidad=_rsi(d1="70.01"), rsi_ratio_btc_diario=Decimal("60"))
    assert sobre.elegible is False
    assert sobre.criterios["tope_sobrecompra"]["cumple"] is False


# --- datos faltantes (P3: nunca se suponen) -----------------------------------


def test_ratio_btc_faltante_no_es_elegible_y_queda_declarado():
    r = evaluar_segunda_ronda(rsi_por_temporalidad=_rsi(), rsi_ratio_btc_diario=None)
    assert r.elegible is False
    assert "rsi_ratio_btc_1d" in r.faltantes


def test_rsi_diario_faltante_no_es_elegible_porque_no_se_puede_verificar_el_tope():
    rsi = _rsi()
    rsi["1d"] = None
    r = evaluar_segunda_ronda(rsi_por_temporalidad=rsi, rsi_ratio_btc_diario=Decimal("60"))
    assert r.elegible is False
    assert "rsi_14_1d" in r.faltantes
    assert r.criterios["tope_sobrecompra"]["cumple"] is False


def test_rsi_faltante_en_una_temporalidad_cuenta_como_sin_momentum_y_se_declara():
    rsi = _rsi()
    rsi["1h"] = None
    r = evaluar_segunda_ronda(rsi_por_temporalidad=rsi, rsi_ratio_btc_diario=Decimal("60"))
    assert r.elegible is True  # aun quedan 3 de 4 con RSI > 50
    assert r.faltantes == ("rsi_14_1h",)


# --- clasificación del tier ---------------------------------------------------


def _ronda(elegible: bool):
    return evaluar_segunda_ronda(
        rsi_por_temporalidad=_rsi(), rsi_ratio_btc_diario=Decimal("60" if elegible else "40")
    )


def test_supera_umbral_es_normal_sin_importar_la_segunda_ronda():
    assert (
        clasificar_tier(puntaje_ponderado=Decimal("55"), supera_umbral_entrada=True, segunda_ronda=None)
        == TierEntrada.NORMAL
    )


def test_en_zona_y_elegible_es_mayor_riesgo():
    assert (
        clasificar_tier(puntaje_ponderado=Decimal("52"), supera_umbral_entrada=False, segunda_ronda=_ronda(True))
        == TierEntrada.MAYOR_RIESGO
    )


def test_en_zona_pero_no_elegible_es_ninguno():
    assert (
        clasificar_tier(puntaje_ponderado=Decimal("52"), supera_umbral_entrada=False, segunda_ronda=_ronda(False))
        == TierEntrada.NINGUNO
    )


def test_fuera_de_zona_es_ninguno_aunque_la_segunda_ronda_fuera_elegible():
    assert (
        clasificar_tier(puntaje_ponderado=Decimal("49.99"), supera_umbral_entrada=False, segunda_ronda=_ronda(True))
        == TierEntrada.NINGUNO
    )


def test_en_zona_sin_segunda_ronda_evaluada_es_ninguno():
    assert (
        clasificar_tier(puntaje_ponderado=Decimal("52"), supera_umbral_entrada=False, segunda_ronda=None)
        == TierEntrada.NINGUNO
    )


# --- observación: ciclos consecutivos en la zona --------------------------------

from app.domain.rules.entry_tier import contar_ciclos_consecutivos_en_zona  # noqa: E402


def _d(valor):
    return None if valor is None else Decimal(valor)


@pytest.mark.parametrize(
    "actual, previos, esperado",
    [
        ("52", [], 1),  # primer día en la zona
        ("52", ["51"], 2),
        ("52", ["51", "50"], 3),
        ("52", ["51", "50", "49.99"], 3),  # 49.99 corta la racha: sale de la zona
        ("52", ["54.99", "55"], 2),  # 55 ya no es zona (supera el umbral)
        ("52", ["60", "51"], 1),  # un día fuera corta; lo anterior no cuenta
        ("52", [None, "51"], 1),  # sin propuesta ese día: no se sabe -> corta (P3)
        ("49.99", ["51", "52"], 0),  # hoy fuera de zona: racha 0
        ("55", ["51"], 0),
        ("50", ["50"], 2),  # borde inferior inclusivo
    ],
)
def test_contar_ciclos_consecutivos_en_zona(actual, previos, esperado):
    assert contar_ciclos_consecutivos_en_zona(Decimal(actual), [_d(p) for p in previos]) == esperado
