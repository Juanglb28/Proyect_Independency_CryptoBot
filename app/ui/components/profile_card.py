"""Tarjeta del perfil de inversión (guardado o propuesto por el entrevistador)."""

from __future__ import annotations

import streamlit as st

from app.ui.components import format as f


def render_profile(p) -> None:
    """`p` tiene los campos del perfil (`PerfilGuardado` o `PerfilPropuesto`); se muestran tal cual."""
    a, b, c, d = st.columns(4)
    a.metric("Horizonte", f"{p.horizonte_meses} meses")
    b.metric("Pérdida máxima tolerable", f.pct(p.perdida_maxima_pct))
    c.metric("Capital sobre patrimonio líquido", f.pct(p.pct_patrimonio))
    d.metric("Experiencia", p.experiencia)
    e, g, h = st.columns(3)
    e.metric("Objetivo de rentabilidad", f.pct(p.objetivo_rentabilidad_pct),
             help="Lo declarado por ti; el sistema no lo estima ni lo valida.")
    g.metric("¿Objetivo realista?", f.si_no(p.objetivo_es_realista), help="Valoración del entrevistador.")
    h.metric("Confianza del perfil", p.confianza_perfil)
    st.markdown(f"**Tolerancia declarada:** {p.tolerancia_declarada}")
    st.markdown(f"**Disponibilidad para aprobar operaciones:** {p.disponibilidad_aprobacion}")
    st.markdown("**Activos que rechaza:** " + (", ".join(p.activos_excluidos) or "ninguno"))
    if p.contradicciones_detectadas:
        st.markdown("**Contradicciones detectadas (sin resolver):**")
        for c_ in p.contradicciones_detectadas:
            st.markdown(f"- {c_}")
