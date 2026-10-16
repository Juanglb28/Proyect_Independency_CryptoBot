"""Tests de la fórmula determinista de calidad de datos (Phase A, tarea A2).

Cubren: caso perfecto (calidad 100), penalización por campos faltantes,
penalización por antigüedad (interpolación lineal, clamp en ambos extremos),
penalización por huecos, combinación de penalizaciones, piso CALIDAD_MINIMA,
las 3 condiciones bloqueantes (lista cerrada), presenta_anomalias como flag
informativo, anomalías sin penalización (eventos), y verificación de constantes.

Patrón: misma estructura que test_prefilter.py — inputs controlados, una sola
aserción por concepto, sin dependencia de DB/red/LLM.
"""

from decimal import Decimal

from app.domain.rules.data_quality import (
    CALIDAD_MINIMA,
    DATA_QUALITY_FORMULA_VERSION,
    HORAS_DATOS_FRESCOS,
    HORAS_DATOS_OBSOLETOS,
    PENALIZACION_MAX_ANTIGUEDAD,
    PENALIZACION_POR_CAMPO_FALTANTE,
    PENALIZACION_POR_HUECO,
    DataQualityResult,
    calculate_data_quality,
)


def D(x) -> Decimal:
    return Decimal(str(x))


def _base_inputs(**overrides) -> dict:
    """Inputs por defecto que producen calidad = 100 (caso perfecto).

    Cualquier test puede sobreescribir UN parámetro para aislar su efecto.
    """
    defaults = dict(
        campos_faltantes_cuantitativo=[],
        campos_faltantes_eventos=[],
        huecos_en_series=[],
        ultima_vela_hace_horas=2.0,  # bien dentro de HORAS_DATOS_FRESCOS
        velas_diarias_disponibles=200,
        eventos_en_catalogo=3,
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Verificación de constantes — protección contra cambios accidentales
# ---------------------------------------------------------------------------


def test_constantes_formula_version():
    assert DATA_QUALITY_FORMULA_VERSION == "1"


def test_constante_penalizacion_por_campo_faltante():
    assert PENALIZACION_POR_CAMPO_FALTANTE == D(3)


def test_constante_horas_datos_frescos():
    assert HORAS_DATOS_FRESCOS == D(26)


def test_constante_horas_datos_obsoletos():
    assert HORAS_DATOS_OBSOLETOS == D(72)


def test_constante_penalizacion_max_antiguedad():
    assert PENALIZACION_MAX_ANTIGUEDAD == D(25)


def test_constante_penalizacion_por_hueco():
    assert PENALIZACION_POR_HUECO == D(5)


def test_constante_calidad_minima():
    assert CALIDAD_MINIMA == D(10)


# ---------------------------------------------------------------------------
# Caso perfecto: calidad = 100
# ---------------------------------------------------------------------------


def test_caso_perfecto_calidad_100():
    result = calculate_data_quality(**_base_inputs())
    assert result.calidad_datos == D(100)
    assert result.penalizacion_campos == D(0)
    assert result.penalizacion_antiguedad == D(0)
    assert result.penalizacion_huecos == D(0)
    assert result.anomalias == ()
    assert result.presenta_anomalias is False
    assert result.bloqueantes == ()
    assert result.formula_version == DATA_QUALITY_FORMULA_VERSION


# ---------------------------------------------------------------------------
# Penalización por campos faltantes
# ---------------------------------------------------------------------------


def test_un_campo_faltante_cuantitativo_resta_3_puntos():
    result = calculate_data_quality(**_base_inputs(
        campos_faltantes_cuantitativo=["rsi_14_4h"],
    ))
    assert result.penalizacion_campos == D(3)
    assert result.calidad_datos == D(97)


def test_un_campo_faltante_eventos_resta_3_puntos():
    result = calculate_data_quality(**_base_inputs(
        campos_faltantes_eventos=["analisis_eventos_para_este_simbolo"],
    ))
    assert result.penalizacion_campos == D(3)
    assert result.calidad_datos == D(97)


def test_campos_faltantes_de_ambos_analistas_se_suman():
    result = calculate_data_quality(**_base_inputs(
        campos_faltantes_cuantitativo=["rsi_14_4h", "atr_14_1h"],
        campos_faltantes_eventos=["analisis_eventos_para_este_simbolo"],
    ))
    # 3 campos × 3 puntos = 9
    assert result.penalizacion_campos == D(9)
    assert result.calidad_datos == D(91)


def test_campos_faltantes_generan_anomalia_informativa():
    result = calculate_data_quality(**_base_inputs(
        campos_faltantes_cuantitativo=["rsi_14_4h"],
    ))
    assert result.presenta_anomalias is True
    assert len(result.anomalias) == 1
    assert "campos_faltantes" in result.anomalias[0]
    assert "rsi_14_4h" in result.anomalias[0]


# ---------------------------------------------------------------------------
# Penalización por antigüedad — interpolación lineal
# ---------------------------------------------------------------------------


def test_antiguedad_dentro_de_horas_frescas_no_penaliza():
    result = calculate_data_quality(**_base_inputs(
        ultima_vela_hace_horas=25.0,  # < 26h
    ))
    assert result.penalizacion_antiguedad == D(0)
    assert result.calidad_datos == D(100)


def test_antiguedad_exactamente_en_horas_frescas_no_penaliza():
    result = calculate_data_quality(**_base_inputs(
        ultima_vela_hace_horas=26.0,  # == HORAS_DATOS_FRESCOS
    ))
    assert result.penalizacion_antiguedad == D(0)


def test_antiguedad_punto_medio_penaliza_la_mitad_del_maximo():
    # Punto medio entre 26h y 72h = 49h → exceso = 23h, rango = 46h
    # penalización = (23/46) * 25 = 12.5
    result = calculate_data_quality(**_base_inputs(
        ultima_vela_hace_horas=49.0,
    ))
    assert result.penalizacion_antiguedad == D("12.5")
    assert result.calidad_datos == D("87.5")


def test_antiguedad_exactamente_en_horas_obsoletas_penaliza_maximo():
    result = calculate_data_quality(**_base_inputs(
        ultima_vela_hace_horas=72.0,
    ))
    assert result.penalizacion_antiguedad == PENALIZACION_MAX_ANTIGUEDAD
    assert result.calidad_datos == D(100) - PENALIZACION_MAX_ANTIGUEDAD


def test_antiguedad_mas_alla_de_obsoletas_se_clampea_al_maximo():
    # 200 horas >> 72h — la penalización no puede superar 25.
    result = calculate_data_quality(**_base_inputs(
        ultima_vela_hace_horas=200.0,
    ))
    assert result.penalizacion_antiguedad == PENALIZACION_MAX_ANTIGUEDAD


def test_antiguedad_genera_anomalia_informativa():
    result = calculate_data_quality(**_base_inputs(
        ultima_vela_hace_horas=50.0,
    ))
    assert result.presenta_anomalias is True
    assert any("datos_antiguos" in a for a in result.anomalias)


# ---------------------------------------------------------------------------
# Penalización por huecos en series
# ---------------------------------------------------------------------------


def test_un_hueco_resta_5_puntos():
    result = calculate_data_quality(**_base_inputs(
        huecos_en_series=["velas_1d: se esperaban ~365, hay 360"],
    ))
    assert result.penalizacion_huecos == D(5)
    assert result.calidad_datos == D(95)


def test_multiples_huecos_se_suman():
    result = calculate_data_quality(**_base_inputs(
        huecos_en_series=["hueco_1d", "hueco_4h", "hueco_1h"],
    ))
    assert result.penalizacion_huecos == D(15)
    assert result.calidad_datos == D(85)


def test_huecos_generan_anomalia_informativa():
    result = calculate_data_quality(**_base_inputs(
        huecos_en_series=["hueco_1d"],
    ))
    assert any("huecos_en_series" in a for a in result.anomalias)


# ---------------------------------------------------------------------------
# Penalizaciones combinadas
# ---------------------------------------------------------------------------


def test_penalizaciones_combinadas_se_suman():
    result = calculate_data_quality(**_base_inputs(
        campos_faltantes_cuantitativo=["rsi_14_4h", "atr_14_4h"],  # 6 pts
        huecos_en_series=["hueco_1d"],                              # 5 pts
        ultima_vela_hace_horas=49.0,                                # 12.5 pts
    ))
    # 6 + 5 + 12.5 = 23.5
    expected_calidad = D(100) - D(6) - D(5) - D("12.5")
    assert result.calidad_datos == expected_calidad
    assert result.calidad_datos == D("76.5")


def test_calidad_nunca_baja_de_CALIDAD_MINIMA():
    # Muchos campos faltantes para forzar penalización > 90 puntos.
    faltantes = [f"campo_{i}" for i in range(35)]  # 35 × 3 = 105 > 90
    result = calculate_data_quality(**_base_inputs(
        campos_faltantes_cuantitativo=faltantes,
    ))
    assert result.calidad_datos == CALIDAD_MINIMA
    assert result.penalizacion_campos == D(35) * PENALIZACION_POR_CAMPO_FALTANTE


def test_calidad_minima_aplica_sobre_penalizaciones_no_bloqueantes():
    # Penalizaciones extremas sin condiciones bloqueantes → piso en 10.
    result = calculate_data_quality(**_base_inputs(
        campos_faltantes_cuantitativo=[f"c_{i}" for i in range(20)],  # 60 pts
        huecos_en_series=[f"h_{i}" for i in range(10)],               # 50 pts
        ultima_vela_hace_horas=100.0,                                  # 25 pts
    ))
    # Total penalización: 135 → 100 - 135 = -35 → clamp a 10
    assert result.calidad_datos == CALIDAD_MINIMA


# ---------------------------------------------------------------------------
# Condiciones bloqueantes (lista cerrada)
# ---------------------------------------------------------------------------


def test_bloqueante_sin_velas_diarias():
    result = calculate_data_quality(**_base_inputs(
        velas_diarias_disponibles=0,
    ))
    assert result.calidad_datos == D(0)
    assert len(result.bloqueantes) >= 1
    assert any("sin_velas_diarias" in b for b in result.bloqueantes)


def test_bloqueante_sin_fecha_ultima_vela():
    result = calculate_data_quality(**_base_inputs(
        ultima_vela_hace_horas=None,
    ))
    assert result.calidad_datos == D(0)
    assert any("sin_fecha_ultima_vela" in b for b in result.bloqueantes)


def test_bloqueante_sin_velas_1d_en_datos():
    result = calculate_data_quality(**_base_inputs(
        campos_faltantes_cuantitativo=["velas_1d"],
    ))
    assert result.calidad_datos == D(0)
    assert any("sin_velas_1d_en_datos" in b for b in result.bloqueantes)


def test_bloqueante_sobreescribe_calidad_minima():
    """Si hay bloqueante, la calidad es 0 incluso si las penalizaciones
    solas habrían dado > CALIDAD_MINIMA."""
    result = calculate_data_quality(**_base_inputs(
        velas_diarias_disponibles=0,
        # Sin otros problemas, la calidad por penalizaciones sería alta.
    ))
    assert result.calidad_datos == D(0)


def test_multiples_bloqueantes_se_acumulan():
    result = calculate_data_quality(**_base_inputs(
        velas_diarias_disponibles=0,
        ultima_vela_hace_horas=None,
        campos_faltantes_cuantitativo=["velas_1d"],
    ))
    assert result.calidad_datos == D(0)
    assert len(result.bloqueantes) == 3


def test_velas_1d_como_faltante_pero_con_otros_faltantes_solo_bloquea_por_velas_1d():
    """El faltante 'velas_1d' en cuantitativo causa bloqueo; otros faltantes
    del mismo analista solo penalizan pero no bloquean."""
    result = calculate_data_quality(**_base_inputs(
        campos_faltantes_cuantitativo=["velas_1d", "rsi_14_4h"],
    ))
    assert result.calidad_datos == D(0)
    bloqueantes_texto = " ".join(result.bloqueantes)
    assert "sin_velas_1d_en_datos" in bloqueantes_texto
    # rsi_14_4h NO es bloqueante
    assert "rsi_14_4h" not in bloqueantes_texto


# ---------------------------------------------------------------------------
# presenta_anomalias — flag informativo (decisión del usuario, 2026-10-08)
# ---------------------------------------------------------------------------


def test_presenta_anomalias_false_cuando_no_hay_anomalias():
    result = calculate_data_quality(**_base_inputs())
    assert result.presenta_anomalias is False
    assert result.anomalias == ()


def test_presenta_anomalias_true_cuando_hay_al_menos_una():
    result = calculate_data_quality(**_base_inputs(
        campos_faltantes_cuantitativo=["rsi_14_4h"],
    ))
    assert result.presenta_anomalias is True
    assert len(result.anomalias) >= 1


def test_sin_eventos_en_catalogo_genera_anomalia_sin_penalizacion():
    """Decisión del usuario: eventos_en_catalogo == 0 es anomalía informativa,
    NO penalización ni bloqueante."""
    result = calculate_data_quality(**_base_inputs(
        eventos_en_catalogo=0,
    ))
    assert result.presenta_anomalias is True
    assert any("sin_eventos_en_catalogo" in a for a in result.anomalias)
    # La calidad NO se afecta — 0 eventos es informativo, no penalización.
    assert result.calidad_datos == D(100)
    assert result.bloqueantes == ()


def test_anomalia_eventos_no_modifica_calidad():
    """Un símbolo con datos perfectos excepto sin eventos debe tener
    calidad = 100 — la anomalía de eventos es solo informativa."""
    result = calculate_data_quality(**_base_inputs(
        eventos_en_catalogo=0,
    ))
    assert result.calidad_datos == D(100)
    assert result.penalizacion_campos == D(0)
    assert result.penalizacion_antiguedad == D(0)
    assert result.penalizacion_huecos == D(0)


# ---------------------------------------------------------------------------
# Resultado: tipos y estructura
# ---------------------------------------------------------------------------


def test_resultado_es_dataclass_frozen():
    result = calculate_data_quality(**_base_inputs())
    assert isinstance(result, DataQualityResult)
    # frozen=True impide modificaciones accidentales
    try:
        result.calidad_datos = D(50)
        assert False, "Debería lanzar FrozenInstanceError"
    except AttributeError:
        pass


def test_resultado_incluye_formula_version():
    result = calculate_data_quality(**_base_inputs())
    assert result.formula_version == DATA_QUALITY_FORMULA_VERSION


def test_anomalias_y_bloqueantes_son_tuplas_inmutables():
    result = calculate_data_quality(**_base_inputs(
        campos_faltantes_cuantitativo=["rsi_14_4h"],
    ))
    assert isinstance(result.anomalias, tuple)
    assert isinstance(result.bloqueantes, tuple)


# ---------------------------------------------------------------------------
# Casos borde
# ---------------------------------------------------------------------------


def test_antiguedad_justo_despues_del_umbral_fresco():
    # 26.001h — apenas pasó el umbral → penalización mínima positiva
    result = calculate_data_quality(**_base_inputs(
        ultima_vela_hace_horas=26.001,
    ))
    assert result.penalizacion_antiguedad > D(0)
    assert result.penalizacion_antiguedad < D(1)  # muy pequeña


def test_todos_los_campos_posibles_faltantes_sin_bloqueante():
    """Muchos faltantes que no incluyen 'velas_1d' → penalización severa
    pero sin bloqueo."""
    faltantes = [f"indicador_{i}" for i in range(30)]
    result = calculate_data_quality(**_base_inputs(
        campos_faltantes_cuantitativo=faltantes,
    ))
    assert result.calidad_datos == CALIDAD_MINIMA  # floor
    assert result.bloqueantes == ()  # sin bloqueo


def test_cero_horas_de_antiguedad_no_penaliza():
    result = calculate_data_quality(**_base_inputs(
        ultima_vela_hace_horas=0.0,
    ))
    assert result.penalizacion_antiguedad == D(0)
