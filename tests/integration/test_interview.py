"""Entrevistador (Agente 1) y servicio de entrevista contra Postgres real, con el
modelo simulado: registro en `llamadas_ia`, errores, cierre a las 8 preguntas y
guardado del perfil (solo-agregar)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.ai.agents import interviewer
from app.ai.agents.interviewer import EntrevistaError, Mensaje, run_interviewer_turn
from app.ai.client import AgentCallResult, AgentResponseError
from app.ai.prompts.interviewer import MENSAJE_DE_CIERRE
from app.config import get_settings
from app.repositories.perfil_usuario import PerfilRow, save_perfil
from app.repositories.ai_calls import get_resumen_llamadas_desde
from app.services import interview, views
from app.services.interview import guardar_perfil, siguiente_turno
from tests.unit.test_interviewer_unit import PERFIL, _conversacion

D = Decimal


class ModeloSimulado:
    """Sustituye a `call_chat`: responde en orden y guarda lo que recibió."""

    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)
        self.llamadas: list[dict] = []

    def __call__(self, settings, *, model, system_prompt, messages, max_tokens):
        self.llamadas.append({"messages": messages, "model": model, "system": system_prompt})
        r = self.respuestas.pop(0)
        if isinstance(r, Exception):
            raise r
        return AgentCallResult(raw_text=r, input_tokens=1000, output_tokens=50)


@pytest.fixture
def modelo(monkeypatch):
    def instalar(*respuestas):
        m = ModeloSimulado(*respuestas)
        monkeypatch.setattr(interviewer, "call_chat", m)
        return m

    return instalar


@pytest.fixture
def cfg():
    return get_settings().model_copy(update={"anthropic_api_key": "test-key-no-real"})


def _llamadas(db_session):
    return db_session.execute(
        text("SELECT agente, exitosa, cost_usd, respuesta, error_message FROM llamadas_ia ORDER BY created_at, id")
    ).all()


# --- agente ---


def test_primera_pregunta_se_registra_con_su_costo(db_session, cfg, modelo):
    m = modelo("¿Cuál es el horizonte temporal previsto para este capital?")
    turno = run_interviewer_turn(db_session, cfg, [])
    assert (turno.tipo, turno.preguntas_hechas) == ("pregunta", 1) and "horizonte" in turno.texto
    (fila,) = _llamadas(db_session)
    assert fila.agente == "entrevistador" and fila.exitosa and fila.cost_usd > 0
    assert fila.respuesta["tipo"] == "pregunta"
    assert m.llamadas[0]["messages"][0]["role"] == "user"


def test_perfil_final_valido(db_session, cfg, modelo):
    modelo(json.dumps(PERFIL))
    turno = run_interviewer_turn(db_session, cfg, _conversacion(3))
    assert turno.tipo == "perfil" and turno.perfil.horizonte_meses == 24 and turno.preguntas_hechas == 3
    assert _llamadas(db_session)[0].respuesta["pct_patrimonio"] == 10


def test_perfil_con_cerca_de_codigo_y_texto_extra_se_tolera(db_session, cfg, modelo):
    modelo("```json\n" + json.dumps(PERFIL) + "\n```")
    assert run_interviewer_turn(db_session, cfg, _conversacion(2)).tipo == "perfil"


def test_perfil_fuera_de_rango_es_error_pero_el_cobro_queda_registrado(db_session, cfg, modelo):
    modelo(json.dumps({**PERFIL, "perdida_maxima_pct": 150}))
    with pytest.raises(EntrevistaError, match="no cumple el formato"):
        run_interviewer_turn(db_session, cfg, _conversacion(3))
    (fila,) = _llamadas(db_session)
    assert fila.exitosa is False and fila.cost_usd > 0 and fila.respuesta is None and fila.error_message


def test_respuesta_vacia_es_error(db_session, cfg, modelo):
    modelo("   ")
    with pytest.raises(EntrevistaError, match="vacío"):
        run_interviewer_turn(db_session, cfg, [])
    assert _llamadas(db_session)[0].exitosa is False


def test_respuesta_cortada_por_max_tokens_cobra_y_falla(db_session, cfg, modelo):
    cortada = AgentCallResult(raw_text='{"horizonte', input_tokens=900, output_tokens=2048)
    modelo(AgentResponseError("cortada", tokens=cortada))
    with pytest.raises(EntrevistaError, match="se cortó"):
        run_interviewer_turn(db_session, cfg, [])
    (fila,) = _llamadas(db_session)
    assert fila.exitosa is False and fila.cost_usd > 0


def test_tras_ocho_preguntas_se_pide_el_cierre(db_session, cfg, modelo):
    m = modelo(json.dumps(PERFIL))
    assert run_interviewer_turn(db_session, cfg, _conversacion(8)).tipo == "perfil"
    assert m.llamadas[0]["messages"][-1]["content"] == MENSAJE_DE_CIERRE


def test_si_tras_el_cierre_sigue_preguntando_no_se_acepta_una_novena(db_session, cfg, modelo):
    modelo("¿Una pregunta más?")
    with pytest.raises(EntrevistaError, match="no cerró el perfil"):
        run_interviewer_turn(db_session, cfg, _conversacion(8))
    assert _llamadas(db_session)[0].exitosa is False


def test_el_costo_entra_en_el_resumen_de_gasto_del_sistema(db_session, cfg, modelo):
    from datetime import datetime, timedelta, timezone

    modelo("Pregunta 1")
    run_interviewer_turn(db_session, cfg, [])
    llamadas, fallidas, costo = get_resumen_llamadas_desde(db_session, datetime.now(timezone.utc) - timedelta(days=1))
    assert (llamadas, fallidas) == (1, 0) and costo > 0


# --- servicio ---


@pytest.fixture
def servicio(db_session, cfg, monkeypatch):
    """Escribe en la sesión de prueba y registra si la transacción terminó sin excepción."""
    salidas = []

    @contextmanager
    def _sesion():
        try:
            yield db_session
        except BaseException as exc:
            salidas.append(exc)
            raise
        else:
            salidas.append(None)

    monkeypatch.setattr(interview, "_abrir_sesion", _sesion)
    monkeypatch.setattr(interview, "settings", lambda: cfg)
    return salidas


def test_servicio_devuelve_la_pregunta(db_session, modelo, servicio):
    modelo("¿Horizonte?")
    r = siguiente_turno(())
    assert (r.ok, r.tipo, r.texto, r.preguntas_hechas) == (True, "pregunta", "¿Horizonte?", 1)


def test_servicio_convierte_el_perfil_con_decimales(db_session, modelo, servicio):
    modelo(json.dumps(PERFIL))
    r = siguiente_turno(tuple(_conversacion(3)))
    assert r.ok and r.tipo == "perfil"
    assert r.perfil.objetivo_rentabilidad_pct == D("30.50") and r.perfil.perdida_maxima_pct == D("20.00")
    assert r.perfil.modelo == interviewer.MODEL_ENTREVISTADOR and r.perfil.prompt_version == "interviewer-v1"


def test_un_fallo_del_agente_no_revierte_el_registro_del_cobro(db_session, modelo, servicio):
    modelo(json.dumps({**PERFIL, "pct_patrimonio": 500}))
    r = siguiente_turno(tuple(_conversacion(3)))
    assert r.ok is False and "formato" in r.mensaje
    assert servicio == [None]  # la transacción se confirma: el cobro de la llamada fallida queda guardado
    assert len(_llamadas(db_session)) == 1


def test_error_inesperado_da_mensaje_claro(db_session, modelo, servicio):
    modelo(RuntimeError("boom con detalle interno"))
    r = siguiente_turno(())
    assert r.ok is False and "boom" not in r.mensaje and "registros" in r.mensaje
    assert isinstance(servicio[0], RuntimeError)


def test_sin_clave_de_api_lo_dice(db_session, monkeypatch, servicio):
    monkeypatch.setattr(interview, "settings", lambda: get_settings().model_copy(update={"anthropic_api_key": None}))
    r = siguiente_turno(())
    assert r.ok is False and "ANTHROPIC_API_KEY" in r.mensaje


def test_respuestas_vacias_o_demasiado_largas_no_llaman_al_modelo(db_session, modelo, servicio):
    m = modelo()
    assert siguiente_turno((Mensaje("agente", "P"), Mensaje("usuario", "   "))).ok is False
    largo = "x" * (interview.MAX_CARACTERES_RESPUESTA_USUARIO + 1)
    assert "demasiado larga" in siguiente_turno((Mensaje("agente", "P"), Mensaje("usuario", largo))).mensaje
    assert m.llamadas == []


def _perfil_propuesto(db_session, modelo):
    modelo(json.dumps(PERFIL))
    return siguiente_turno(tuple(_conversacion(3))).perfil


def test_guardar_perfil_es_el_vigente_y_el_anterior_queda_como_historia(db_session, modelo, servicio, monkeypatch):
    p = _perfil_propuesto(db_session, modelo)
    assert guardar_perfil(p).ok is True
    from app.services.profile import get_perfil_view

    v = get_perfil_view(db_session)
    assert v.total_perfiles == 1 and v.vigente.horizonte_meses == 24 and v.vigente.activos_excluidos == ("XRP",)
    assert v.vigente.objetivo_rentabilidad_pct == D("30.50")

    otro = interview.PerfilPropuesto(**{**p.__dict__, "horizonte_meses": 36})
    guardar_perfil(otro)
    v = get_perfil_view(db_session)
    assert v.total_perfiles == 2 and v.vigente.horizonte_meses == 36


def test_la_base_rechaza_modificar_o_borrar_un_perfil(db_session, modelo, servicio):
    guardar_perfil(_perfil_propuesto(db_session, modelo))
    with pytest.raises(DBAPIError, match="solo-agregar"):
        with db_session.begin_nested():
            db_session.execute(text("UPDATE perfil_usuario SET horizonte_meses = 1"))
    with pytest.raises(DBAPIError, match="solo-agregar"):
        with db_session.begin_nested():
            db_session.execute(text("DELETE FROM perfil_usuario"))


def test_la_base_rechaza_valores_fuera_de_rango(db_session, modelo, servicio):
    p = _perfil_propuesto(db_session, modelo)
    campos = {k: v for k, v in p.__dict__.items()}
    campos["activos_excluidos"] = list(p.activos_excluidos)
    campos["contradicciones_detectadas"] = list(p.contradicciones_detectadas)
    for cambio in ({"pct_patrimonio": D("150")}, {"perdida_maxima_pct": D("-1")}, {"horizonte_meses": 0},
                   {"experiencia": "experto"}, {"confianza_perfil": "total"}):
        with pytest.raises(DBAPIError):
            with db_session.begin_nested():
                save_perfil(db_session, PerfilRow(**{**campos, **cambio}))


def test_la_fachada_lee_el_perfil_en_solo_lectura(db_engine, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("app.services.session._factory", lambda: factory)
    v = views.perfil()
    assert v.total_perfiles == 0 or v.vigente is not None
