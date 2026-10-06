"""Perfil de inversión: entrevista con el Agente 1 (Paso 10b).

El perfil es INFORMATIVO: no cambia el análisis, el score ni las decisiones del
sistema. La conversación vive en la sesión del navegador; solo se guarda el
perfil final, y solo si lo confirmas.
"""

from __future__ import annotations

import streamlit as st

from app.services import interview, views
from app.services.interview import Mensaje
from app.ui.components import format as f
from app.ui.components.confirm_action import confirm_action, show_last_action_result
from app.ui.components.profile_card import render_profile

_ACTIVA, _HISTORIAL, _PERFIL, _ERROR = "entrevista_activa", "entrevista_historial", "entrevista_perfil", "entrevista_error"


def _reiniciar(activa: bool) -> None:
    st.session_state[_ACTIVA] = activa
    st.session_state[_HISTORIAL] = []
    st.session_state[_PERFIL] = None
    st.session_state[_ERROR] = None


def _turno() -> None:
    historial: list[Mensaje] = st.session_state[_HISTORIAL]
    with st.spinner("El entrevistador está preparando el siguiente paso…"):
        r = interview.siguiente_turno(tuple(historial))
    st.session_state[_ERROR] = None if r.ok else r.mensaje
    if r.ok and r.tipo == "pregunta":
        historial.append(Mensaje("agente", r.texto))
    elif r.ok:
        st.session_state[_PERFIL] = r.perfil


def render() -> None:
    show_last_action_result()
    st.title("Perfil de inversión")
    st.caption("Informativo: el perfil no cambia el análisis, el score ni las decisiones del sistema. "
               "Se guarda solo en tu base de datos local.")

    v = views.perfil()
    if v.vigente is None:
        st.info("Todavía no hay un perfil guardado.")
    else:
        st.subheader("Perfil vigente")
        st.caption(f"Entrevista del {f.fecha(v.vigente.creado_en)} · {v.total_perfiles} perfil(es) guardado(s) en total.")
        render_profile(v.vigente)

    st.divider()
    st.subheader("Entrevista")
    if not st.session_state.get(_ACTIVA, False):
        st.caption(
            f"Hasta {interview.MAX_PREGUNTAS} preguntas, una a la vez. Usa el modelo de IA (cuesta centavos): tus "
            "respuestas se envían a Anthropic. No se guardan como texto; solo el perfil final y solo si lo confirmas."
        )
        if st.button("Rehacer la entrevista" if v.vigente else "Iniciar entrevista"):
            _reiniciar(True)
            _turno()
            st.rerun()
        return

    historial: list[Mensaje] = st.session_state[_HISTORIAL]
    for m in historial:
        with st.chat_message("assistant" if m.rol == "agente" else "user"):
            st.write(m.texto)
    preguntas = sum(1 for m in historial if m.rol == "agente")
    perfil = st.session_state.get(_PERFIL)
    if perfil is None:
        st.caption(f"Pregunta {min(preguntas, interview.MAX_PREGUNTAS)} de {interview.MAX_PREGUNTAS}")

    error = st.session_state.get(_ERROR)
    if error:
        st.error(error)
        if st.button("Reintentar"):
            _turno()
            st.rerun()
    elif perfil is not None:
        st.subheader("Perfil propuesto")
        st.caption("Lo armó el modelo con tus respuestas. Revísalo: solo se guarda si lo confirmas.")
        render_profile(perfil)

        def _guardar(_motivo):
            resultado = interview.guardar_perfil(perfil)
            if resultado.ok:
                _reiniciar(False)
            return resultado

        confirm_action(
            "guardar-perfil", "Guardar este perfil",
            "Vas a GUARDAR este perfil como el vigente. El anterior se conserva como historia. Es solo informativo: "
            "no cambia ninguna decisión del sistema.",
            _guardar,
        )
    else:
        respuesta = st.chat_input("Tu respuesta", max_chars=interview.MAX_CARACTERES_RESPUESTA_USUARIO)
        if respuesta:
            historial.append(Mensaje("usuario", respuesta))
            _turno()
            st.rerun()

    if st.button("Cancelar la entrevista"):
        _reiniciar(False)
        st.rerun()
