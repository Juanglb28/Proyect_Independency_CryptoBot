"""Catálogo de definiciones (sección 8.1): completo, consistente y versionado."""
from app.ui.definitions import CATALOG_VERSION, DEFINITION_CATALOG, Definition, DefinitionCatalog, TipoDeDato

CLAVES_DEL_PLAN = {
    "rsi", "atr", "relative_volume", "moving_averages", "distance_ath", "distance_90d_high",
    "mae", "mfe", "r_multiple", "expectancy", "profit_factor", "max_drawdown",
    "opportunity_score", "risk_score", "confidence_score",
}

# Si este test falla porque cambió un texto: sube `version` de esa definición
# y `CATALOG_VERSION`, y actualiza esta huella. Así la explicación mostrada en
# pantalla es reproducible históricamente (sección 8.1).
HUELLA_ESPERADA = "0025ad9fe724a0399c201cc9cdd86b1e3403aa4a6bd05165be2aa6913887a3e0"


def test_estan_las_quince_definiciones_iniciales_del_plan():
    assert set(DEFINITION_CATALOG.keys()) == CLAVES_DEL_PLAN


def test_cada_definicion_tiene_todos_los_campos_con_contenido():
    for key in DEFINITION_CATALOG.keys():
        d = DEFINITION_CATALOG.get(key)
        assert isinstance(d, Definition) and d.key == key
        for campo in ("display_name", "short_description", "interpretation", "unit", "limitations", "version"):
            assert getattr(d, campo).strip(), f"{key}.{campo} vacío"
        assert isinstance(d.kind, TipoDeDato)


def test_metricas_de_evaluacion_se_distinguen_de_datos_de_decision():
    posteriores = {k for k in CLAVES_DEL_PLAN if DEFINITION_CATALOG.get(k).kind == TipoDeDato.EVALUACION_POSTERIOR}
    assert posteriores == {"mae", "mfe", "r_multiple", "expectancy", "profit_factor", "max_drawdown"}


def test_risk_y_confidence_score_declaran_que_no_estan_calculados():
    for key in ("risk_score", "confidence_score"):
        assert "no lo calcula" in DEFINITION_CATALOG.get(key).short_description or "no hay un número" in (
            DEFINITION_CATALOG.get(key).short_description
        )


def test_clave_desconocida_no_inventa_tooltip():
    assert DEFINITION_CATALOG.get("no_existe") is None
    assert DEFINITION_CATALOG.tooltip_text("no_existe") is None


def test_tooltip_incluye_unidad_limitaciones_tipo_y_version():
    texto = DEFINITION_CATALOG.tooltip_text("mae")
    assert "Unidad:" in texto and "Limitaciones:" in texto and "evaluación posterior" in texto and "v1" in texto


def test_claves_repetidas_se_rechazan():
    d = DEFINITION_CATALOG.get("rsi")
    try:
        DefinitionCatalog((d, d), "1")
    except ValueError:
        return
    raise AssertionError("debió rechazar claves repetidas")


def test_la_huella_del_catalogo_exige_subir_la_version_si_cambia_un_texto():
    assert CATALOG_VERSION == "1"
    assert DEFINITION_CATALOG.fingerprint() == HUELLA_ESPERADA, (
        "El contenido del catálogo cambió: sube la versión y actualiza HUELLA_ESPERADA. "
        f"Huella actual: {DEFINITION_CATALOG.fingerprint()}"
    )
