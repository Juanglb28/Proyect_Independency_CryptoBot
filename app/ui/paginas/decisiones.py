"""Decisiones: historial de ciclos y trazabilidad de cada uno (solo lectura)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.services import views
from app.ui.components import format as f
from app.ui.components.cycle_detail import render_cycle_detail


def render() -> None:
    st.title("Decisiones")
    st.caption("Qué se evaluó, qué se propuso y qué pasó con cada propuesta, tal como quedó guardado entonces.")
    ciclos = views.ciclos(limit=30)
    if not ciclos:
        st.info("Todavía no se ha ejecutado ningún ciclo de análisis.")
        return
    st.dataframe(
        pd.DataFrame({"Inicio": [f.fecha(c.started_at) for c in ciclos],
                      "Resultado": [c.resultado.value for c in ciclos],
                      "Última etapa": [c.ultima_etapa.value for c in ciclos],
                      "Propuestas": [c.n_propuestas for c in ciclos],
                      "Sobre el umbral": [c.n_sobre_umbral for c in ciclos],
                      "Observaciones abiertas": [c.n_observaciones for c in ciclos],
                      "Costo IA (USD)": [f.num(c.cost_usd, 4) for c in ciclos]}),
        hide_index=True,
    )
    etiquetas = {f"{f.fecha(c.started_at)} · {c.resultado.value}": c.ciclo_id for c in ciclos}
    elegido = st.selectbox("Ver el detalle de un ciclo", list(etiquetas))
    detalle = views.ciclo_detalle(etiquetas[elegido])
    if detalle is not None:
        render_cycle_detail(detalle)
