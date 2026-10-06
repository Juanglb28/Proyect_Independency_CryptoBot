"""Tesis y criterio de invalidación frente al resultado (lo que se sabía vs lo que pasó)."""

from __future__ import annotations

import streamlit as st

from app.ui.components import format as f


def render_thesis_result(decision, fila) -> None:
    if decision is None:
        st.warning("Esta operación no tiene propuesta asociada: no hay tesis congelada que mostrar.")
        return
    st.markdown(f"**Tesis al entrar:** {decision.tesis}")
    st.markdown(f"**Qué la invalidaría:** {decision.invalidacion}")
    st.markdown(
        f"**Qué pasó:** {fila.resultado.value} ({f.pct(fila.net_return_pct)}) · "
        f"estado de la tesis: **{fila.thesis_status.value}** · salida por **{fila.exit_reason.value}**"
    )
    st.caption("La tesis y la invalidación son las guardadas al momento de la decisión; no se reescriben.")
