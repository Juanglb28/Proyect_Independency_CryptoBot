"""Tests de app.ai.models: cálculo de costo por llamada."""

from decimal import Decimal

import pytest

from app.ai.models import MODEL_AUDITOR, calculate_cost_usd


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
