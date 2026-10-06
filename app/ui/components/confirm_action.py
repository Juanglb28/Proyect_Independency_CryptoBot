"""Acciones con confirmación en dos pasos.

Un botón nunca ejecuta de inmediato: primero muestra QUÉ va a pasar y pide
confirmar o cancelar. El resultado (éxito o motivo de rechazo) se guarda en la
sesión y se muestra una sola vez al inicio de la página, aun cuando la acción
haga desaparecer el elemento (p. ej. una entrada pendiente ya resuelta).
"""

from __future__ import annotations

from collections.abc import Callable

import streamlit as st

_ULTIMO = "ultimo_resultado_accion"


def show_last_action_result() -> None:
    """Muestra (y olvida) el resultado de la última acción confirmada."""
    resultado = st.session_state.pop(_ULTIMO, None)
    if resultado is None:
        return
    (st.success if resultado.ok else st.warning)(resultado.mensaje)


def confirm_action(
    key: str,
    label: str,
    summary: str,
    action: Callable[[str | None], object],
    *,
    disabled_reason: str | None = None,
    ask_reason: bool = False,
) -> None:
    """Botón `label` -> resumen y Confirmar/Cancelar -> `action(motivo)`.

    `action` devuelve un resultado con `.ok` y `.mensaje`. Con
    `disabled_reason` el botón se muestra deshabilitado con esa explicación.
    """
    estado = f"confirmar::{key}"
    if not st.session_state.get(estado, False):
        if st.button(label, key=f"abrir::{key}", disabled=disabled_reason is not None):
            st.session_state[estado] = True
            st.rerun()
        if disabled_reason:
            st.caption(disabled_reason)
        return

    st.warning(summary)
    motivo = st.text_input("Motivo (opcional)", key=f"motivo::{key}") if ask_reason else None
    confirmar, cancelar = st.columns(2)
    if confirmar.button("Confirmar", key=f"confirmar-btn::{key}", type="primary"):
        st.session_state[estado] = False
        st.session_state[_ULTIMO] = action(motivo)
        st.rerun()
    if cancelar.button("Cancelar", key=f"cancelar-btn::{key}"):
        st.session_state[estado] = False
        st.rerun()
