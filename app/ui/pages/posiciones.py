"""Posiciones abiertas y entradas pendientes de aprobación (solo lectura)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.services import views
from app.ui.components import format as f
from app.ui.components.indicator_tooltip import render_indicator_label


def render() -> None:
    v = views.posiciones()
    st.title("Posiciones")
    st.caption(f"{len(v.posiciones)} abiertas de un máximo de {v.maximo_posiciones} · "
               f"cupos libres: {v.cupos_libres} · última observación: {f.fecha(v.ultima_observacion_en)}")
    if v.parada_emergencia:
        st.error("PARADA DE EMERGENCIA activa.")
    elif v.sistema_pausado:
        st.warning("Sistema PAUSADO: no se abren entradas nuevas.")

    if not v.posiciones:
        st.info("No hay posiciones abiertas.")
    for p in v.posiciones:
        with st.expander(f"{p.symbol} · abierta {f.fecha(p.opened_at)}", expanded=True):
            a, b, c, d = st.columns(4)
            a.metric("Precio de entrada", f.precio(p.entry_price))
            b.metric("Último precio", f.precio(p.ultimo_precio))
            c.metric("P&L no realizado", f.usd(p.pnl_no_realizado), help=f"Retorno {f.pct(p.retorno_no_realizado_pct)}")
            d.metric("Cantidad restante", f.precio(p.remaining_quantity), help=f"Inicial {f.precio(p.initial_quantity)}")
            e, g, h, i = st.columns(4)
            e.metric("Stop vigente", f.precio(p.stop_vigente), help=f"Distancia: {f.pct(p.distancia_stop_pct)}")
            g.metric("Objetivo vigente", f.precio(p.objetivo_vigente), help=f"Distancia: {f.pct(p.distancia_objetivo_pct)}")
            with h:
                render_indicator_label("mae", f.pct(p.mae_pct), label="MAE parcial")
            with i:
                render_indicator_label("mfe", f.pct(p.mfe_pct), label="MFE parcial")
            st.caption(f"Take profit parcial ejecutado: {f.si_no(p.take_profit_hit)} · trailing activo: "
                       f"{f.si_no(p.trailing_active)} · última observación: {f.fecha(p.ultima_observacion_en)}")

    st.subheader("Entradas pendientes de aprobación")
    if not v.entradas_pendientes:
        st.caption("No hay entradas pendientes.")
    else:
        st.dataframe(
            pd.DataFrame({"Símbolo": [e.symbol for e in v.entradas_pendientes],
                          "Monto (USD)": [f.num(e.requested_usd) for e in v.entradas_pendientes],
                          "Precio de referencia": [f.precio(e.reference_price) for e in v.entradas_pendientes],
                          "Creada": [f.fecha(e.created_at) for e in v.entradas_pendientes],
                          "Expira": [f.fecha(e.expires_at) for e in v.entradas_pendientes]}),
            hide_index=True,
        )
        st.caption("Aprobar o rechazar se hace por terminal (`scripts.entries`); esta pantalla es de solo lectura.")
