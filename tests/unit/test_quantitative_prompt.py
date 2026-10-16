"""El prompt del Agente 2.

v2 (2026-10-08): las medias y el volumen son de PERÍODOS de cada temporalidad (plan §6.1),
no de "días". v3 (2026-10-15): convención de tiempos (la fecha de cada vela es su APERTURA y
cada temporalidad cierra en un momento distinto); nada más cambia."""

from app.ai.prompts.quantitative import PROMPT_VERSION, SYSTEM_PROMPT


def test_la_version_del_prompt_cambio_con_la_redaccion():
    assert PROMPT_VERSION == "v3"


def test_el_prompt_explica_la_convencion_de_tiempos_sin_pedir_calcular_horas():
    assert "CONVENCIÓN DE TIEMPOS" in SYSTEM_PROMPT
    assert "hora de APERTURA de la última vela CERRADA" in SYSTEM_PROMPT
    assert "Nada de lo anterior es desfase, antigüedad ni incoherencia" in SYSTEM_PROMPT
    assert "no son cifras del bloque DATOS" in SYSTEM_PROMPT


def test_la_convencion_de_tiempos_va_antes_de_la_salida_y_no_altera_el_formato():
    assert SYSTEM_PROMPT.index("CONVENCIÓN DE TIEMPOS") < SYSTEM_PROMPT.index("SALIDA")
    assert "Solo JSON, sin texto adicional" in SYSTEM_PROMPT
    for criterio in ("CRITERIO CLAVE DE ENTRADA", "QUÉ DEBES EVALUAR", "PROHIBICIONES ABSOLUTAS"):
        assert criterio in SYSTEM_PROMPT


def test_el_prompt_habla_de_periodos_y_no_de_dias_para_medias_y_volumen():
    assert "90, 120 y 180 períodos" in SYSTEM_PROMPT
    assert "media de 30 períodos" in SYSTEM_PROMPT
    assert "180 días" not in SYSTEM_PROMPT
    assert "media de 30 días" not in SYSTEM_PROMPT


def test_las_prohibiciones_de_p3_siguen_intactas():
    assert "No produzcas ninguna cifra que no aparezca literalmente en el bloque DATOS" in SYSTEM_PROMPT
    assert "No estimes, aproximes ni infieras valores ausentes" in SYSTEM_PROMPT
