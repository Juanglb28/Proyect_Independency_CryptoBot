"""Acciones con confirmación en dos pasos.

Un botón nunca ejecuta de inmediato: primero muestra QUÉ va a pasar y pide
confirmar o cancelar. El resultado (éxito o motivo de rechazo) se guarda en la
sesión y se muestra una sola vez al inicio de la página, aun cuando la acción
haga desaparecer el elemento (p. ej. una entrada pendiente ya resuelta).

En las acciones sin vuelta atrás el segundo paso puede ser un deslizador
(`slide_to_confirm`): la acción solo se ejecuta cuando se lleva hasta el final.
Es solo presentación: la acción, sus validaciones y sus bloqueos son los mismos.
"""

from __future__ import annotations

from collections.abc import Callable

import streamlit as st

_ULTIMO = "ultimo_resultado_accion"
_DESLIZADOR_FINAL = 100  # la acción se ejecuta solo al llevar el deslizador a este valor


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
    confirm_label: str = "Confirmar",
    irreversible: str | None = None,
    slide_to_confirm: bool = False,
) -> None:
    """Botón `label` -> resumen y Confirmar/Cancelar -> `action(motivo)`.

    `confirm_label` rotula el segundo botón (p. ej. "Sí, abrir la posición") para
    que no parezca una repetición del primero; `irreversible` muestra, junto a
    ese botón, un aviso destacado de que la acción no se puede deshacer.
    Con `slide_to_confirm` el segundo paso es un deslizador rotulado `confirm_label`
    (en vez del botón de confirmar): solo al llevarlo hasta el final se ejecuta.

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
    if irreversible:
        st.error(irreversible)
    motivo = st.text_input("Motivo (opcional)", key=f"motivo::{key}") if ask_reason else None
    if slide_to_confirm:
        avance = st.slider(
            confirm_label, 0, _DESLIZADOR_FINAL, 0, key=f"deslizar::{key}", format="%d %%",
            help="Arrastra la perilla hasta el 100 % para confirmar. Si la sueltas antes, no pasa nada.",
        )
        ejecutar = avance >= _DESLIZADOR_FINAL
        zona_cancelar = st.container()
    else:
        confirmar, zona_cancelar = st.columns(2)
        ejecutar = confirmar.button(confirm_label, key=f"confirmar-btn::{key}", type="primary")
    if ejecutar:
        st.session_state[estado] = False
        st.session_state[_ULTIMO] = action(motivo)
        st.rerun()
    if zona_cancelar.button("Cancelar", key=f"cancelar-btn::{key}"):
        st.session_state[estado] = False
        st.rerun()
