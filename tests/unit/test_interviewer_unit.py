"""Entrevistador (Agente 1): armado de la conversación y esquema del perfil, sin red ni base."""

import pytest
from pydantic import ValidationError

from app.ai.agents.interviewer import MAX_PREGUNTAS, Mensaje, _parece_perfil, construir_mensajes
from app.ai.prompts.interviewer import MENSAJE_DE_CIERRE, MENSAJE_DE_INICIO, SYSTEM_PROMPT
from app.ai.schemas import PerfilUsuarioOutput

PERFIL = {
    "horizonte_meses": 24, "perdida_maxima_pct": 20, "experiencia": "basica", "activos_excluidos": ["XRP"],
    "tolerancia_declarada": "Aguanto caídas", "objetivo_rentabilidad_pct": 30.5, "objetivo_es_realista": True,
    "disponibilidad_aprobacion": "Diaria", "pct_patrimonio": 10, "contradicciones_detectadas": [],
    "confianza_perfil": "media",
}


def _conversacion(preguntas: int) -> list[Mensaje]:
    historial = []
    for i in range(preguntas):
        historial += [Mensaje("agente", f"P{i + 1}"), Mensaje("usuario", f"R{i + 1}")]
    return historial


def test_el_primer_turno_solo_lleva_el_mensaje_de_inicio():
    assert construir_mensajes([]) == [{"role": "user", "content": MENSAJE_DE_INICIO}]


def test_la_conversacion_alterna_roles_y_termina_en_usuario():
    msgs = construir_mensajes(_conversacion(2))
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    assert msgs[-1]["content"] == "R2"


def test_con_ocho_preguntas_el_codigo_pide_el_cierre():
    msgs = construir_mensajes(_conversacion(MAX_PREGUNTAS))
    assert msgs[-1] == {"role": "user", "content": MENSAJE_DE_CIERRE}
    assert construir_mensajes(_conversacion(MAX_PREGUNTAS - 1))[-1]["content"] == f"R{MAX_PREGUNTAS - 1}"


def test_un_historial_que_termina_en_el_agente_es_un_error():
    with pytest.raises(ValueError):
        construir_mensajes([Mensaje("agente", "P1")])


@pytest.mark.parametrize("texto,esperado", [
    ('{"a": 1}', True), ('  \n{"a": 1}', True), ('```json\n{"a": 1}\n```', True),
    ("¿Cuál es tu horizonte?", False), ("Entiendo. {no es perfil}", False),
])
def test_distingue_pregunta_de_perfil(texto, esperado):
    assert _parece_perfil(texto) is esperado


def test_el_prompt_es_el_del_plan_con_su_limite_de_ocho_preguntas():
    assert "máximo de 8 preguntas" in SYSTEM_PROMPT and "contradicciones_detectadas" in SYSTEM_PROMPT


def test_el_esquema_acepta_el_perfil_del_plan():
    assert PerfilUsuarioOutput.model_validate(PERFIL).experiencia == "basica"


@pytest.mark.parametrize("cambio", [
    {"perdida_maxima_pct": 150}, {"pct_patrimonio": -1}, {"horizonte_meses": 0}, {"objetivo_rentabilidad_pct": -5},
    {"experiencia": "experto"}, {"confianza_perfil": "total"}, {"campo_extra": 1},
])
def test_el_esquema_rechaza_valores_fuera_de_rango_o_campos_extra(cambio):
    with pytest.raises(ValidationError):
        PerfilUsuarioOutput.model_validate({**PERFIL, **cambio})


def test_el_esquema_exige_todos_los_campos():
    incompleto = {k: v for k, v in PERFIL.items() if k != "pct_patrimonio"}
    with pytest.raises(ValidationError):
        PerfilUsuarioOutput.model_validate(incompleto)
