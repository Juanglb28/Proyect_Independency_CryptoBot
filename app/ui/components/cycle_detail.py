"""Detalle de un ciclo de análisis: qué se evaluó, qué se propuso y qué pasó."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.ui.components import format as f


def render_cycle_detail(d, *, con_llamadas: bool = True) -> None:
    r = d.resumen
    st.markdown(f"**Ciclo del {f.fecha(r.started_at)}** · resultado **{r.resultado.value}** · "
                f"última etapa `{r.ultima_etapa.value}` · costo IA {f.usd(r.cost_usd, 4)}")
    if r.error_message:
        st.error(f"El ciclo falló: {r.error_message}")
    st.markdown("**Candidatos evaluados por la IA:** " + (", ".join(d.candidatos_evaluados) or "ninguno registrado"))
    st.caption(d.nota_embudo)
    if d.recomendacion is not None:
        st.markdown(f"**Recomendación del Consolidador:** {d.recomendacion} · confianza {d.confianza_global or f.ND}")
        if d.razon_no_invertir:
            st.markdown(f"**Razón para no invertir:** {d.razon_no_invertir}")
    if not d.propuestas:
        st.info("Este ciclo no produjo propuestas.")
    for p in d.propuestas:
        estado = "supera el umbral de entrada" if p.supera_umbral_entrada else "no supera el umbral de entrada"
        with st.expander(f"{p.symbol} · score {f.num(p.puntaje_ponderado)} · {estado}"):
            a, b, c = st.columns(3)
            a.metric("Puntaje base", f.num(p.puntaje_base), help=f"Calidad de datos aplicada: {f.num(p.calidad_datos)}")
            b.metric("Confianza global", p.confianza_global)
            c.metric("Tier de entrada", p.tier_entrada or f.ND)
            st.markdown(f"**Tesis:** {p.tesis}")
            st.markdown(f"**Invalidación:** {p.invalidacion}")
            if p.comentario_chartista:
                st.markdown(f"**Comentario del chartista:** {p.comentario_chartista}")
            if p.factores:
                st.dataframe(
                    pd.DataFrame({"Factor": [x.factor for x in p.factores],
                                  "Peso": [f.num(x.peso) for x in p.factores],
                                  "Puntaje": [f.num(x.puntaje) for x in p.factores]}),
                    hide_index=True,
                )
            st.caption(f"Orden de entrada: {p.estado_entrada or 'ninguna'} · posición: {p.estado_posicion or 'ninguna'}")
    if con_llamadas and d.llamadas:
        with st.expander(f"Llamadas a la IA ({len(d.llamadas)})"):
            st.dataframe(
                pd.DataFrame({"Agente": [x.agente for x in d.llamadas], "Modelo": [x.modelo for x in d.llamadas],
                              "Prompt": [x.prompt_version for x in d.llamadas],
                              "Tokens (ent./sal.)": [f"{x.input_tokens}/{x.output_tokens}" for x in d.llamadas],
                              "Costo (USD)": [f.num(x.cost_usd, 4) for x in d.llamadas],
                              "Exitosa": [f.si_no(x.exitosa) for x in d.llamadas],
                              "Error": [x.error_message or "" for x in d.llamadas]}),
                hide_index=True,
            )
