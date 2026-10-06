"""Validación de la estrategia: ¿el score predice algo? (solo lectura)."""

from __future__ import annotations

import streamlit as st

from app.services import views
from app.ui.components import format as f
from app.ui.components.score_analysis import render_tercile_table, render_validation_report


def render() -> None:
    v = views.validacion()
    st.title("Validación de estrategia")
    st.caption("El objetivo de la v1 no es ganar dinero sino medir si el score tiene capacidad predictiva. "
               f"Historial desde {f.fecha(v.periodo_inicio)}.")
    render_validation_report(v.informe)
    st.subheader("Terciles de score")
    render_tercile_table(v.terciles)
    st.subheader("Las seis preguntas de validación")
    for q in v.preguntas:
        marca = "Disponible" if q.estado.value == "DISPONIBLE" else "PENDIENTE"
        with st.expander(f"{q.numero}. {q.texto} — {marca}"):
            st.write(q.nota)
