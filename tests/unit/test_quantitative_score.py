"""Tests del score cuantitativo paralelo (Fase B — shadow scoring).

Cubren: normalización de los 4 factores (tendencia, volumen, distancia
max 90d, RSI), combinación con pesos iguales, datos faltantes (deben
devolver `None`, nunca un valor de relleno), comportamiento de clamp en
los extremos, y estructura del resultado.

Siguen el mismo patrón de `test_prefilter.py` y `test_data_quality.py`:
helper `_base_inputs(**overrides)` para mantener los tests concisos.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.domain.rules.quantitative_score import (
    DISTANCIA_MAX_90D_PCT_BOUNDS,
    QUANTITATIVE_FORMULA_VERSION,
    RSI_BOUNDS,
    TENDENCIA_PCT_BOUNDS,
    VOLUMEN_RELATIVO_BOUNDS,
    QuantitativeScore,
    _normalize_linear,
    calculate_quantitative_score,
)


def D(x) -> Decimal:
    return Decimal(str(x))


_T = datetime(2026, 10, 8, tzinfo=timezone.utc)


def _base_inputs(**overrides):
    """Inputs por defecto que producen un score en el punto medio (50) para
    cada factor — todos los sub-scores valen 50, el final también.

    Punto medio de cada factor:
    - tendencia: 0% (centro de [-25, +25])
    - volumen relativo: 1.0 (centro de [0.4, 1.6])
    - distancia max 90d: -10% (centro de [-20, 0])
    - RSI: 50 (centro de [30, 70])
    """
    defaults = {
        "symbol": "ETHUSDT",
        "calculated_at": _T,
        "sma_pct_values": [D(0), D(0), D(0)],  # promedio = 0%
        "volumen_relativo": D("1.0"),
        "distancia_max_90d_pct": D("-10"),
        "rsi": D("50"),
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------


def test_formula_version_is_set():
    assert QUANTITATIVE_FORMULA_VERSION == "1"


def test_bounds_match_expected_values():
    """Verifica que los bounds no cambien silenciosamente — si cambian,
    la versión de la fórmula también debe subir."""
    assert TENDENCIA_PCT_BOUNDS == (D(-25), D(25))
    assert VOLUMEN_RELATIVO_BOUNDS == (D("0.4"), D("1.6"))
    assert DISTANCIA_MAX_90D_PCT_BOUNDS == (D(-20), D(0))
    assert RSI_BOUNDS == (D(30), D(70))


# ---------------------------------------------------------------------------
# _normalize_linear (misma que en prefilter, duplicada a propósito)
# ---------------------------------------------------------------------------


def test_normalize_linear_maps_bounds_to_0_and_100():
    bounds = (D(-25), D(25))
    assert _normalize_linear(D(-25), bounds) == D(0)
    assert _normalize_linear(D(25), bounds) == D(100)
    assert _normalize_linear(D(0), bounds) == D(50)


def test_normalize_linear_clamps_values_outside_bounds():
    bounds = (D(-25), D(25))
    assert _normalize_linear(D(40), bounds) == D(100)
    assert _normalize_linear(D(-100), bounds) == D(0)


def test_normalize_linear_rsi_bounds():
    """RSI [30, 70]: 30 → 0, 50 → 50, 70 → 100."""
    bounds = RSI_BOUNDS
    assert _normalize_linear(D(30), bounds) == D(0)
    assert _normalize_linear(D(50), bounds) == D(50)
    assert _normalize_linear(D(70), bounds) == D(100)
    # Extremos clampeados
    assert _normalize_linear(D(10), bounds) == D(0)
    assert _normalize_linear(D(90), bounds) == D(100)


def test_normalize_linear_rejects_invalid_bounds():
    with pytest.raises(ValueError, match="bounds inválidos"):
        _normalize_linear(D(0), (D(10), D(10)))
    with pytest.raises(ValueError, match="bounds inválidos"):
        _normalize_linear(D(0), (D(10), D(5)))


# ---------------------------------------------------------------------------
# Punto medio — todos los factores en el centro de sus bounds
# ---------------------------------------------------------------------------


def test_midpoint_all_factors_produce_score_50():
    result = calculate_quantitative_score(**_base_inputs())
    assert result is not None
    assert result.score_tendencia == D(50)
    assert result.score_volumen == D(50)
    assert result.score_distancia == D(50)
    assert result.score_rsi == D(50)
    assert result.score_final == D(50)


# ---------------------------------------------------------------------------
# Máximo — todos los factores saturados en el extremo alto
# ---------------------------------------------------------------------------


def test_max_all_factors_produce_score_100():
    result = calculate_quantitative_score(**_base_inputs(
        sma_pct_values=[D(30), D(30), D(30)],  # > +25 → clamp a 100
        volumen_relativo=D("2.0"),               # > 1.6 → clamp a 100
        distancia_max_90d_pct=D("5"),            # > 0 → clamp a 100
        rsi=D("80"),                             # > 70 → clamp a 100
    ))
    assert result is not None
    assert result.score_tendencia == D(100)
    assert result.score_volumen == D(100)
    assert result.score_distancia == D(100)
    assert result.score_rsi == D(100)
    assert result.score_final == D(100)


# ---------------------------------------------------------------------------
# Mínimo — todos los factores saturados en el extremo bajo
# ---------------------------------------------------------------------------


def test_min_all_factors_produce_score_0():
    result = calculate_quantitative_score(**_base_inputs(
        sma_pct_values=[D(-40), D(-40), D(-40)],  # < -25 → clamp a 0
        volumen_relativo=D("0.1"),                  # < 0.4 → clamp a 0
        distancia_max_90d_pct=D("-30"),             # < -20 → clamp a 0
        rsi=D("15"),                                # < 30 → clamp a 0
    ))
    assert result is not None
    assert result.score_tendencia == D(0)
    assert result.score_volumen == D(0)
    assert result.score_distancia == D(0)
    assert result.score_rsi == D(0)
    assert result.score_final == D(0)


# ---------------------------------------------------------------------------
# Factores individuales — uno al máximo, el resto en el punto medio
# ---------------------------------------------------------------------------


def test_single_factor_tendencia_max_rest_midpoint():
    """Solo tendencia al máximo (100), resto en 50 → final = (100+50+50+50)/4 = 62.5"""
    result = calculate_quantitative_score(**_base_inputs(
        sma_pct_values=[D(25), D(25), D(25)],
    ))
    assert result is not None
    assert result.score_tendencia == D(100)
    assert result.score_final == D("62.5")


def test_single_factor_volumen_max_rest_midpoint():
    result = calculate_quantitative_score(**_base_inputs(
        volumen_relativo=D("1.6"),
    ))
    assert result is not None
    assert result.score_volumen == D(100)
    assert result.score_final == D("62.5")


def test_single_factor_distancia_max_rest_midpoint():
    result = calculate_quantitative_score(**_base_inputs(
        distancia_max_90d_pct=D("0"),
    ))
    assert result is not None
    assert result.score_distancia == D(100)
    assert result.score_final == D("62.5")


def test_single_factor_rsi_max_rest_midpoint():
    result = calculate_quantitative_score(**_base_inputs(
        rsi=D("70"),
    ))
    assert result is not None
    assert result.score_rsi == D(100)
    assert result.score_final == D("62.5")


# ---------------------------------------------------------------------------
# Pesos iguales — verificar que cada factor contribuye exactamente 25%
# ---------------------------------------------------------------------------


def test_equal_weights_each_factor_contributes_25_percent():
    """Un solo factor en 100, resto en 0 → final = 25 (= 100/4)."""
    # Solo tendencia
    r1 = calculate_quantitative_score(**_base_inputs(
        sma_pct_values=[D(25), D(25), D(25)],
        volumen_relativo=D("0.4"),
        distancia_max_90d_pct=D("-20"),
        rsi=D("30"),
    ))
    assert r1 is not None
    assert r1.score_final == D(25)

    # Solo RSI
    r2 = calculate_quantitative_score(**_base_inputs(
        sma_pct_values=[D(-25), D(-25), D(-25)],
        volumen_relativo=D("0.4"),
        distancia_max_90d_pct=D("-20"),
        rsi=D("70"),
    ))
    assert r2 is not None
    assert r2.score_final == D(25)


# ---------------------------------------------------------------------------
# Datos faltantes — devolver None (P3: cero datos inventados)
# ---------------------------------------------------------------------------


def test_missing_sma_values_returns_none():
    assert calculate_quantitative_score(**_base_inputs(sma_pct_values=[])) is None


def test_sma_with_none_element_returns_none():
    assert calculate_quantitative_score(**_base_inputs(
        sma_pct_values=[D(0), None, D(0)],
    )) is None


def test_missing_volumen_relativo_returns_none():
    assert calculate_quantitative_score(**_base_inputs(volumen_relativo=None)) is None


def test_missing_distancia_max_90d_returns_none():
    assert calculate_quantitative_score(**_base_inputs(distancia_max_90d_pct=None)) is None


def test_missing_rsi_returns_none():
    assert calculate_quantitative_score(**_base_inputs(rsi=None)) is None


def test_all_none_returns_none():
    assert calculate_quantitative_score(**_base_inputs(
        sma_pct_values=[None, None, None],
        volumen_relativo=None,
        distancia_max_90d_pct=None,
        rsi=None,
    )) is None


# ---------------------------------------------------------------------------
# Clamp — valores extremos saturan en 0 o 100, no se desbordan
# ---------------------------------------------------------------------------


def test_clamp_tendencia_high():
    """Tendencia +40% clampea igual que +25%."""
    r1 = calculate_quantitative_score(**_base_inputs(
        sma_pct_values=[D(40), D(40), D(40)],
    ))
    r2 = calculate_quantitative_score(**_base_inputs(
        sma_pct_values=[D(25), D(25), D(25)],
    ))
    assert r1 is not None and r2 is not None
    assert r1.score_tendencia == r2.score_tendencia == D(100)


def test_clamp_rsi_low():
    """RSI de 5 clampea igual que RSI de 30."""
    r1 = calculate_quantitative_score(**_base_inputs(rsi=D(5)))
    r2 = calculate_quantitative_score(**_base_inputs(rsi=D(30)))
    assert r1 is not None and r2 is not None
    assert r1.score_rsi == r2.score_rsi == D(0)


# ---------------------------------------------------------------------------
# Estructura del resultado
# ---------------------------------------------------------------------------


def test_result_is_frozen_dataclass():
    result = calculate_quantitative_score(**_base_inputs())
    assert result is not None
    assert isinstance(result, QuantitativeScore)
    with pytest.raises(AttributeError):
        result.score_final = D(99)  # type: ignore[misc]


def test_result_carries_raw_values_and_formula_version():
    result = calculate_quantitative_score(**_base_inputs())
    assert result is not None
    assert result.symbol == "ETHUSDT"
    assert result.calculated_at == _T
    assert result.tendencia_pct == D(0)
    assert result.volumen_relativo == D("1.0")
    assert result.distancia_max_90d_pct == D("-10")
    assert result.rsi == D("50")
    assert result.formula_version == QUANTITATIVE_FORMULA_VERSION


# ---------------------------------------------------------------------------
# Caso realista — valores observados en datos reales
# ---------------------------------------------------------------------------


def test_realistic_values():
    """Un caso con valores plausibles del mercado real (no extremos)."""
    result = calculate_quantitative_score(**_base_inputs(
        symbol="SOLUSDT",
        sma_pct_values=[D("5.2"), D("8.1"), D("12.3")],  # promedio ~8.53%
        volumen_relativo=D("1.15"),
        distancia_max_90d_pct=D("-6.3"),
        rsi=D("58.4"),
    ))
    assert result is not None
    assert result.symbol == "SOLUSDT"

    # Tendencia: (5.2+8.1+12.3)/3 = 8.533... → normalize [-25,+25]
    # = (8.533..+25)/50 * 100 = 67.066...
    assert result.score_tendencia > D(67) and result.score_tendencia < D(68)

    # Volumen: 1.15 → normalize [0.4,1.6] = (1.15-0.4)/1.2 * 100 = 62.5
    assert result.score_volumen == D("62.5")

    # Distancia: -6.3 → normalize [-20,0] = (-6.3+20)/20 * 100 = 68.5
    assert result.score_distancia == D("68.5")

    # RSI: 58.4 → normalize [30,70] = (58.4-30)/40 * 100 = 71
    assert result.score_rsi == D(71)

    # Final: promedio de los 4 sub-scores
    expected_final = (
        result.score_tendencia + result.score_volumen
        + result.score_distancia + result.score_rsi
    ) / 4
    assert result.score_final == expected_final
    assert result.formula_version == "1"


# ---------------------------------------------------------------------------
# Tendencia: promedio de SMA distances
# ---------------------------------------------------------------------------


def test_tendencia_averages_sma_distances():
    """El factor tendencia es el promedio de las distancias a SMA, no una
    sola de ellas."""
    result = calculate_quantitative_score(**_base_inputs(
        sma_pct_values=[D(-10), D(0), D(10)],
    ))
    assert result is not None
    assert result.tendencia_pct == D(0)  # (-10 + 0 + 10) / 3 = 0
    assert result.score_tendencia == D(50)  # 0 es el centro de [-25, +25]


# ---------------------------------------------------------------------------
# Sin multiplicador de calidad — score_final no depende de calidad_datos
# ---------------------------------------------------------------------------


def test_no_calidad_datos_multiplier():
    """El score cuantitativo NO tiene multiplicador de calidad de datos.
    El resultado es puramente de indicadores."""
    result = calculate_quantitative_score(**_base_inputs())
    assert result is not None
    # No existe atributo calidad_datos en el resultado
    assert not hasattr(result, "calidad_datos")
    assert not hasattr(result, "puntaje_ponderado")
