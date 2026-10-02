"""Tests de app.ai.models: cálculo de costo por llamada."""

from decimal import Decimal

import pytest

from app.ai.models import MODEL_ANALISTA, MODEL_AUDITOR, MODEL_CONSOLIDADOR, calculate_cost_usd


def test_calculate_cost_usd_matches_hand_calculation():
    # Haiku 4.5: 1 USD/MTok entrada, 5 USD/MTok salida.
    # 1000 tokens entrada, 500 tokens salida:
    # (1000 * 1 + 500 * 5) / 1_000_000 = 3500 / 1_000_000 = 0.0035
    cost = calculate_cost_usd(MODEL_AUDITOR, input_tokens=1000, output_tokens=500)
    assert cost == Decimal("0.0035")


def test_calculate_cost_usd_zero_tokens_is_zero_cost():
    assert calculate_cost_usd(MODEL_AUDITOR, input_tokens=0, output_tokens=0) == Decimal(0)


def test_calculate_cost_usd_raises_on_unknown_model():
    with pytest.raises(KeyError):
        calculate_cost_usd("un-modelo-que-no-existe", input_tokens=100, output_tokens=100)


def test_calculate_cost_usd_sin_argumentos_de_cache_no_cambia_nada():
    # Backward-compat: una llamada que no pasa los parámetros de cache
    # (default 0) debe costar exactamente lo mismo que antes del fix.
    cost = calculate_cost_usd(MODEL_AUDITOR, input_tokens=1000, output_tokens=500)
    cost_explicito = calculate_cost_usd(
        MODEL_AUDITOR,
        input_tokens=1000,
        output_tokens=500,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    assert cost == cost_explicito


def test_calculate_cost_usd_cobra_la_escritura_de_cache_a_1_25x_el_precio_de_entrada():
    # Bug real (2026-10-02): antes de este fix, cache_creation_input_tokens
    # no se cobraba en absoluto. Sonnet 5.5: 2 USD/MTok entrada.
    # 1000 tokens de escritura de cache a 1.25x = 1000 * 2 * 1.25 / 1e6
    cost = calculate_cost_usd(
        MODEL_ANALISTA, input_tokens=0, output_tokens=0, cache_creation_input_tokens=1000
    )
    assert cost == Decimal("0.0025")


def test_calculate_cost_usd_cobra_la_lectura_de_cache_a_0_1x_para_sonnet():
    # Sonnet 5.5: 1000 tokens de lectura de cache a 0.1x = 1000 * 2 * 0.1 / 1e6
    cost = calculate_cost_usd(MODEL_ANALISTA, input_tokens=0, output_tokens=0, cache_read_input_tokens=1000)
    assert cost == Decimal("0.0002")


def test_calculate_cost_usd_lectura_de_cache_de_opus_usa_0_05x_no_el_0_1x_por_defecto():
    # Excepción documentada: Opus 5.5 lee cache a 0.05x, no al 0.1x estándar
    # de los demás modelos. Consolidador (Opus 5.5): 4 USD/MTok entrada.
    # 1000 tokens de lectura a 0.05x = 1000 * 4 * 0.05 / 1e6
    cost = calculate_cost_usd(
        MODEL_CONSOLIDADOR, input_tokens=0, output_tokens=0, cache_read_input_tokens=1000
    )
    assert cost == Decimal("0.0002")
