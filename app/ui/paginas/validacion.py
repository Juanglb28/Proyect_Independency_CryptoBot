"""Validación de la estrategia: ¿el score predice algo? (solo lectura)."""

from __future__ import annotations

import streamlit as st

from app.services import views
from app.ui.components import format as f
from app.ui.components.factor_analysis import render_factor_analysis
from app.ui.components.score_analysis import (
    render_stratified_report,
    render_tercile_table,
    render_validation_report,
)


def render() -> None:
    v = views.validacion()
    st.title("Validación de estrategia")
    st.caption("El objetivo de la v1 no es ganar dinero sino medir si el score tiene capacidad predictiva. "
               f"Historial desde {f.fecha(v.periodo_inicio)}.")
    render_validation_report(v.informe)
    st.subheader("Medición adicional: estratificada por mes")
    render_stratified_report(v.estratificado)
    st.subheader("Serie de observación (aparte)")
    st.caption("Candidatos del pre-filtro abiertos sin IA, 100 USD cada uno. Su puntaje es el del PRE-FILTRO (otra escala "
               "que el ponderado de la estrategia), por eso se mide por separado y nunca sustituye a la validación principal.")
    if v.informe_observacion is not None:
        render_validation_report(v.informe_observacion)
    if v.estratificado_observacion is not None:
        with st.expander("Medición estratificada por mes de la serie de observación"):
            render_stratified_report(v.estratificado_observacion)
    if v.informe_conjunto is not None:
        with st.expander("Comparación conjunta (estrategia + observación)"):
            st.caption("Solo para comparar: mezcla dos escalas de puntaje distintas; no es una validación.")
            render_validation_report(v.informe_conjunto)
    st.subheader("Terciles de score (estrategia)")
    render_tercile_table(v.terciles)
    st.subheader("Análisis por factor")
    render_factor_analysis(v.factores)
    st.subheader("Las seis preguntas de validación")
    for q in v.preguntas:
        marca = "Disponible" if q.estado.value == "DISPONIBLE" else "PENDIENTE"
        with st.expander(f"{q.numero}. {q.texto} — {marca}"):
            st.write(q.nota)
