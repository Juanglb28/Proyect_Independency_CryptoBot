"""Tests de app.domain.enums (Paso 7)."""

from app.domain.enums import EstadoCiclo


def test_estado_ciclo_tiene_exactamente_las_cinco_etapas_de_analisis():
    # Deliberadamente NO incluye INGEST/VALIDATE_DATA/CALCULATE_INDICATORS
    # (procesos separados y manuales, ver README) ni RISK_VALIDATION en
    # adelante (Paso 8/9) — ver el docstring de EstadoCiclo. Este test
    # falla a propósito si alguien agrega una etapa sin código real que la
    # transite (sección 30 del master prompt).
    assert [e.value for e in EstadoCiclo] == [
        "PREFILTER",
        "EVENT_CORRELATION",
        "AI_ANALYSIS",
        "CONSOLIDATE",
        "FREEZE_DECISION_SNAPSHOT",
    ]


def test_estado_ciclo_es_string_para_persistir_directo_como_texto():
    assert EstadoCiclo.PREFILTER == "PREFILTER"
    assert isinstance(EstadoCiclo.PREFILTER.value, str)
