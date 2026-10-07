"""Análisis: lo que evaluó y propuso el último ciclo."""

from __future__ import annotations

import streamlit as st

from app.services import views
from app.ui.components.cycle_detail import render_cycle_detail


def render() -> None:
    st.title("Análisis")
    st.caption("Último ciclo de análisis, tal como quedó guardado.")
    ciclos = views.ciclos(limit=1)
    if not ciclos:
        st.info("Todavía no se ha ejecutado ningún ciclo de análisis.")
    else:
        detalle = views.ciclo_detalle(ciclos[0].ciclo_id)
        if detalle is not None:
            render_cycle_detail(detalle, con_llamadas=False)
    st.divider()
    st.markdown("**Análisis por factor:** está en la página *Validación de estrategia* (sección 8.8).")
