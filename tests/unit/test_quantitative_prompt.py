"""El prompt del Agente 2 (v2, 2026-10-08): las medias y el volumen son de
PERÍODOS de cada temporalidad (plan §6.1), no de "días"."""

from app.ai.prompts.quantitative import PROMPT_VERSION, SYSTEM_PROMPT


def test_la_version_del_prompt_cambio_con_la_redaccion():
    assert PROMPT_VERSION == "v2"


def test_el_prompt_habla_de_periodos_y_no_de_dias_para_medias_y_volumen():
    assert "90, 120 y 180 períodos" in SYSTEM_PROMPT
    assert "media de 30 períodos" in SYSTEM_PROMPT
    assert "180 días" not in SYSTEM_PROMPT
    assert "media de 30 días" not in SYSTEM_PROMPT


def test_las_prohibiciones_de_p3_siguen_intactas():
    assert "No produzcas ninguna cifra que no aparezca literalmente en el bloque DATOS" in SYSTEM_PROMPT
    assert "No estimes, aproximes ni infieras valores ausentes" in SYSTEM_PROMPT
