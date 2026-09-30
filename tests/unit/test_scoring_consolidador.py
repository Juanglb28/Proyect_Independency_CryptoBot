"""Tests de app.domain.rules.scoring.calculate_consolidated_score (Paso 6,
Agente 5 — Consolidador). Verifica que la combinación de las 4
sub-puntuaciones y el multiplicador de calidad de datos sea exactamente la
fórmula fija acordada (30/20/20/30, calidad como multiplicador), no algo
que dependa del juicio del LLM.
"""

from decimal import Decimal

import pytest

from app.domain.rules.scoring import SCORE_MINIMO_ENTRADA, calculate_consolidated_score


def D(x) -> Decimal:
    return Decimal(str(x))


def test_matches_the_hand_worked_example_from_the_design_discussion():
    # score_base = 82 (mismo valor en las 4 categorías, así puntaje_base ==
    # ese valor sin importar los pesos exactos); calidad_datos=70 ->
    # 82 * 0.70 = 57.4
    result = calculate_consolidated_score(
        tendencia_momentum=D(82),
        volumen_liquidez=D(82),
        posicion_rangos_ath=D(82),
        contexto_eventos=D(82),
        calidad_datos=D(70),
    )
    assert result.puntaje_base == D(82)
    assert result.puntaje_ponderado == D("57.400")
    assert result.supera_umbral_entrada is True  # 57.4 >= 55


def test_applies_30_20_20_30_weights():
    # tendencia=100, volumen=0, rangos=0, eventos=0 -> puntaje_base = 30
    result = calculate_consolidated_score(
        tendencia_momentum=D(100),
        volumen_liquidez=D(0),
        posicion_rangos_ath=D(0),
        contexto_eventos=D(0),
        calidad_datos=D(100),
    )
    assert result.puntaje_base == D(30)

    # eventos=100, resto 0 -> también 30 (mismo peso que tendencia)
    result2 = calculate_consolidated_score(
        tendencia_momentum=D(0),
        volumen_liquidez=D(0),
        posicion_rangos_ath=D(0),
        contexto_eventos=D(100),
        calidad_datos=D(100),
    )
    assert result2.puntaje_base == D(30)

    # volumen=100, resto 0 -> 20 (peso menor que tendencia/eventos)
    result3 = calculate_consolidated_score(
        tendencia_momentum=D(0),
        volumen_liquidez=D(100),
        posicion_rangos_ath=D(0),
        contexto_eventos=D(0),
        calidad_datos=D(100),
    )
    assert result3.puntaje_base == D(20)


def test_low_data_quality_can_sink_an_otherwise_excellent_score():
    # Exactamente el caso que motivó la fórmula: un activo con indicadores
    # excelentes pero evidencia débil no debe terminar con un puntaje alto.
    result = calculate_consolidated_score(
        tendencia_momentum=D(94),
        volumen_liquidez=D(94),
        posicion_rangos_ath=D(94),
        contexto_eventos=D(94),
        calidad_datos=D(40),
    )
    assert result.puntaje_base == D(94)
    assert result.puntaje_ponderado == D("37.600")
    assert result.supera_umbral_entrada is False


def test_perfect_score_and_perfect_data_quality_hits_100():
    result = calculate_consolidated_score(
        tendencia_momentum=D(100),
        volumen_liquidez=D(100),
        posicion_rangos_ath=D(100),
        contexto_eventos=D(100),
        calidad_datos=D(100),
    )
    assert result.puntaje_ponderado == D(100)


def test_score_exactly_at_threshold_counts_as_passing():
    # 55 exacto debe pasar (>=, no >) — mismo criterio que el pre-filtro.
    result = calculate_consolidated_score(
        tendencia_momentum=D(55),
        volumen_liquidez=D(55),
        posicion_rangos_ath=D(55),
        contexto_eventos=D(55),
        calidad_datos=D(100),
    )
    assert result.puntaje_ponderado == D(55)
    assert result.supera_umbral_entrada is True
    assert SCORE_MINIMO_ENTRADA == 55


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tendencia_momentum": D(101), "volumen_liquidez": D(0), "posicion_rangos_ath": D(0), "contexto_eventos": D(0), "calidad_datos": D(100)},
        {"tendencia_momentum": D(0), "volumen_liquidez": D(-1), "posicion_rangos_ath": D(0), "contexto_eventos": D(0), "calidad_datos": D(100)},
        {"tendencia_momentum": D(0), "volumen_liquidez": D(0), "posicion_rangos_ath": D(0), "contexto_eventos": D(0), "calidad_datos": D(150)},
    ],
)
def test_rejects_values_outside_0_100_instead_of_silently_clamping(kwargs):
    with pytest.raises(ValueError):
        calculate_consolidated_score(**kwargs)
