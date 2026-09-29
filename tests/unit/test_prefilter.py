"""Tests del pre-filtro determinista (sección 4.2/6.1 del plan, Paso 6.1).

Cubren: normalización con límites fijos (clamp en ambos extremos),
combinación de los tres factores con pesos iguales, símbolos con datos
faltantes (deben quedar SIN puntaje, nunca con un valor de relleno), y la
selección de candidatos (umbral, tope, desempate determinista, y el caso
legítimo de cero candidatos).
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.data.prefilter import (
    DISTANCIA_MAX_90D_PCT_BOUNDS,
    TENDENCIA_PCT_BOUNDS,
    VOLUMEN_RELATIVO_BOUNDS,
    PrefilterScore,
    _normalize_linear,
    calculate_prefilter_score,
    select_candidates,
)


def D(x) -> Decimal:
    return Decimal(str(x))


_T = datetime(2026, 9, 29, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# _normalize_linear
# ---------------------------------------------------------------------------


def test_normalize_linear_maps_bounds_to_0_and_100():
    bounds = (D(-25), D(25))
    assert _normalize_linear(D(-25), bounds) == D(0)
    assert _normalize_linear(D(25), bounds) == D(100)
    assert _normalize_linear(D(0), bounds) == D(50)


def test_normalize_linear_clamps_values_outside_bounds():
    bounds = (D(-25), D(25))
    # Un valor muy por fuera del límite no vale "más" ni "menos" que el
    # límite mismo: ambos saturan en el extremo correspondiente.
    assert _normalize_linear(D(40), bounds) == D(100)
    assert _normalize_linear(D(-100), bounds) == D(100) - D(100)  # == 0
    assert _normalize_linear(D(-1000), bounds) == D(0)


def test_normalize_linear_handles_bounds_not_centered_on_zero():
    # distancia_max_90d: [-20, 0] — nunca es positivo en la práctica.
    bounds = (D(-20), D(0))
    assert _normalize_linear(D(-20), bounds) == D(0)
    assert _normalize_linear(D(0), bounds) == D(100)
    assert _normalize_linear(D(-10), bounds) == D(50)


# ---------------------------------------------------------------------------
# calculate_prefilter_score
# ---------------------------------------------------------------------------


def test_calculate_prefilter_score_averages_sma_percentages_as_tendencia():
    # Tres SMA con %-vs-precio de 10, 20, 30 -> tendencia = 20.
    score = calculate_prefilter_score(
        "BTCUSDT",
        _T,
        sma_pct_values=[D(10), D(20), D(30)],
        volumen_relativo=D(1.0),
        distancia_max_90d_pct=D(-10),
    )
    assert score is not None
    assert score.tendencia_pct == D(20)


def test_calculate_prefilter_score_combines_three_factors_with_equal_weights():
    # tendencia=0 (score 50), volumen=1.0 -> centro exacto de [0.4,1.6]
    # (score 50), distancia=-10 -> centro exacto de [-20,0] (score 50).
    # Los tres factores en su punto medio deben dar exactamente 50.
    score = calculate_prefilter_score(
        "ETHUSDT",
        _T,
        sma_pct_values=[D(0), D(0), D(0)],
        volumen_relativo=D(1.0),
        distancia_max_90d_pct=D(-10),
    )
    assert score is not None
    assert score.score_tendencia == D(50)
    assert score.score_volumen == D(50)
    assert score.score_distancia == D(50)
    assert score.score_final == D(50)


def test_calculate_prefilter_score_best_case_saturates_at_100():
    score = calculate_prefilter_score(
        "SOLUSDT",
        _T,
        sma_pct_values=[D(25), D(25), D(25)],
        volumen_relativo=D(1.6),
        distancia_max_90d_pct=D(0),
    )
    assert score is not None
    assert score.score_final == D(100)


def test_calculate_prefilter_score_returns_none_when_any_sma_missing():
    score = calculate_prefilter_score(
        "XRPUSDT",
        _T,
        sma_pct_values=[D(10), None, D(30)],
        volumen_relativo=D(1.0),
        distancia_max_90d_pct=D(-10),
    )
    assert score is None


def test_calculate_prefilter_score_returns_none_when_sma_list_empty():
    score = calculate_prefilter_score(
        "XRPUSDT",
        _T,
        sma_pct_values=[],
        volumen_relativo=D(1.0),
        distancia_max_90d_pct=D(-10),
    )
    assert score is None


def test_calculate_prefilter_score_returns_none_when_volumen_relativo_missing():
    score = calculate_prefilter_score(
        "XRPUSDT",
        _T,
        sma_pct_values=[D(10), D(20), D(30)],
        volumen_relativo=None,
        distancia_max_90d_pct=D(-10),
    )
    assert score is None


def test_calculate_prefilter_score_returns_none_when_distancia_missing():
    score = calculate_prefilter_score(
        "XRPUSDT",
        _T,
        sma_pct_values=[D(10), D(20), D(30)],
        volumen_relativo=D(1.0),
        distancia_max_90d_pct=None,
    )
    assert score is None


def test_bounds_constants_match_the_documented_first_cut():
    # Si alguien cambia estos límites sin querer, este test lo marca: son
    # una decisión explícita, no un accidente de refactor.
    assert TENDENCIA_PCT_BOUNDS == (D(-25), D(25))
    assert VOLUMEN_RELATIVO_BOUNDS == (D(0.4), D(1.6))
    assert DISTANCIA_MAX_90D_PCT_BOUNDS == (D(-20), D(0))


# ---------------------------------------------------------------------------
# select_candidates
# ---------------------------------------------------------------------------


def _score(symbol: str, score_final: Decimal) -> PrefilterScore:
    return PrefilterScore(
        symbol=symbol,
        calculated_at=_T,
        tendencia_pct=D(0),
        volumen_relativo=D(1.0),
        distancia_max_90d_pct=D(-10),
        score_tendencia=D(50),
        score_volumen=D(50),
        score_distancia=D(50),
        score_final=score_final,
    )


def test_select_candidates_filters_by_minimum_score():
    scores = [_score("BTCUSDT", D(60)), _score("ETHUSDT", D(44))]
    candidates = select_candidates(scores)
    assert [c.symbol for c in candidates] == ["BTCUSDT"]


def test_select_candidates_can_return_empty_list_on_a_weak_market_day():
    # Caso explícitamente previsto en la sección 4.2 del plan: el ciclo
    # puede terminar sin un solo candidato.
    scores = [_score("BTCUSDT", D(10)), _score("ETHUSDT", D(20))]
    assert select_candidates(scores) == []


def test_select_candidates_sorts_by_score_descending():
    scores = [_score("BTCUSDT", D(60)), _score("ETHUSDT", D(90)), _score("SOLUSDT", D(75))]
    candidates = select_candidates(scores)
    assert [c.symbol for c in candidates] == ["ETHUSDT", "SOLUSDT", "BTCUSDT"]


def test_select_candidates_caps_at_max_candidatos_prefiltro():
    scores = [_score(f"SYM{i}USDT", D(100 - i)) for i in range(8)]
    candidates = select_candidates(scores)
    assert len(candidates) == 5
    assert [c.symbol for c in candidates] == [
        "SYM0USDT",
        "SYM1USDT",
        "SYM2USDT",
        "SYM3USDT",
        "SYM4USDT",
    ]


def test_select_candidates_breaks_ties_by_symbol_ascending_for_determinism():
    scores = [_score("ETHUSDT", D(60)), _score("BTCUSDT", D(60)), _score("SOLUSDT", D(60))]
    candidates = select_candidates(scores)
    assert [c.symbol for c in candidates] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_select_candidates_score_exactly_at_threshold_is_included():
    # >= , no >: el umbral mismo debe calificar (sección 19: "Score mínimo
    # prefilter: 45").
    scores = [_score("BTCUSDT", D(45))]
    candidates = select_candidates(scores)
    assert [c.symbol for c in candidates] == ["BTCUSDT"]
