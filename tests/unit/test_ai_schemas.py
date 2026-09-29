"""Tests de contrato de los schemas Pydantic contra la forma EXACTA de
"SALIDA" que especifica cada prompt (sección 7.2 del plan) — sección 15.8,
nivel "contrato": "esquemas JSON de los agentes y validación de
respuestas".
"""

import pytest
from pydantic import ValidationError

from app.ai.schemas import AnalistaCuantitativoOutput, AnalistaEventosOutput, AuditorOutput


# ---------------------------------------------------------------------------
# Agente 2 — Analista cuantitativo
# ---------------------------------------------------------------------------


def test_analista_cuantitativo_accepts_a_well_formed_response():
    payload = {
        "simbolo": "BTCUSDT",
        "observaciones": [
            {
                "factor": "tendencia",
                "lectura": "precio 18% sobre SMA_180",
                "direccion": "favorable",
                "fuerza": 4,
                "campos_usados": ["close_1d", "sma_180_1d"],
            }
        ],
        "datos_faltantes": [],
        "conflictos_entre_temporalidades": "ninguno relevante",
        "confianza": "alta",
        "justificacion_confianza": "las 4 temporalidades coinciden en dirección",
    }
    output = AnalistaCuantitativoOutput.model_validate(payload)
    assert output.observaciones[0].direccion == "favorable"


def test_analista_cuantitativo_rejects_invalid_direccion_value():
    payload = {
        "simbolo": "BTCUSDT",
        "observaciones": [
            {
                "factor": "tendencia",
                "lectura": "x",
                "direccion": "alcista",  # no es una de las 3 opciones del prompt
                "fuerza": 4,
                "campos_usados": [],
            }
        ],
        "datos_faltantes": [],
        "conflictos_entre_temporalidades": "",
        "confianza": "alta",
        "justificacion_confianza": "",
    }
    with pytest.raises(ValidationError):
        AnalistaCuantitativoOutput.model_validate(payload)


def test_analista_cuantitativo_rejects_unexpected_extra_field():
    payload = {
        "simbolo": "BTCUSDT",
        "observaciones": [],
        "datos_faltantes": [],
        "conflictos_entre_temporalidades": "",
        "confianza": "baja",
        "justificacion_confianza": "",
        "precio_objetivo": 123,  # el agente 2 nunca debe proponer precios
    }
    with pytest.raises(ValidationError):
        AnalistaCuantitativoOutput.model_validate(payload)


def test_analista_cuantitativo_rejects_missing_required_field():
    payload = {
        "simbolo": "BTCUSDT",
        "observaciones": [],
        "datos_faltantes": [],
        "conflictos_entre_temporalidades": "",
        # falta "confianza" y "justificacion_confianza"
    }
    with pytest.raises(ValidationError):
        AnalistaCuantitativoOutput.model_validate(payload)


# ---------------------------------------------------------------------------
# Agente 3 — Analista de eventos
# ---------------------------------------------------------------------------


def test_analista_eventos_accepts_a_well_formed_response():
    payload = {
        "simbolo": "XRPUSDT",
        "eventos_comparables": [
            {
                "evento_id": "ripple-sec-case-ends",
                "semejanza": "media",
                "razon_semejanza": "resolución regulatoria favorable",
                "comportamiento_posterior": "+15% en 7 días tras el evento",
            }
        ],
        "tamano_muestra": 1,
        "consistencia_historica": "mixta",
        "lectura_general": "muestra insuficiente para conclusiones firmes",
        "confianza": "baja",
        "anomalias_detectadas": [],
    }
    output = AnalistaEventosOutput.model_validate(payload)
    assert output.tamano_muestra == 1


def test_analista_eventos_rejects_invalid_consistencia_value():
    payload = {
        "simbolo": "XRPUSDT",
        "eventos_comparables": [],
        "tamano_muestra": 0,
        "consistencia_historica": "no_aplica",  # no es una de las 3 opciones
        "lectura_general": "",
        "confianza": "baja",
        "anomalias_detectadas": [],
    }
    with pytest.raises(ValidationError):
        AnalistaEventosOutput.model_validate(payload)


def test_analista_eventos_captures_prompt_injection_reports():
    # El campo que existe justamente para que el agente reporte una
    # inyección detectada en el texto de un evento (P4 del plan) debe
    # aceptar contenido libre sin que el schema lo bloquee.
    payload = {
        "simbolo": "ETHUSDT",
        "eventos_comparables": [],
        "tamano_muestra": 0,
        "consistencia_historica": "contradictoria",
        "lectura_general": "sin datos suficientes",
        "confianza": "baja",
        "anomalias_detectadas": [
            "la descripción del evento 'x' contenía una instrucción para ignorar "
            "las prohibiciones del sistema; fue ignorada"
        ],
    }
    output = AnalistaEventosOutput.model_validate(payload)
    assert len(output.anomalias_detectadas) == 1


# ---------------------------------------------------------------------------
# Agente 4 — Auditor de datos
# ---------------------------------------------------------------------------


def test_auditor_accepts_a_well_formed_response():
    payload = {
        "campos_faltantes": [],
        "huecos_en_series": [],
        "valores_anomalos": [],
        "antiguedad_maxima_horas": 2.5,
        "calidad_global": "suficiente",
        "penalizacion_confianza_pct": 0,
        "bloqueantes": [],
    }
    output = AuditorOutput.model_validate(payload)
    assert output.calidad_global == "suficiente"


def test_auditor_rejects_invalid_calidad_global_value():
    payload = {
        "campos_faltantes": [],
        "huecos_en_series": [],
        "valores_anomalos": [],
        "antiguedad_maxima_horas": 2.5,
        "calidad_global": "excelente",  # no es una de las 3 opciones del prompt
        "penalizacion_confianza_pct": 0,
        "bloqueantes": [],
    }
    with pytest.raises(ValidationError):
        AuditorOutput.model_validate(payload)
