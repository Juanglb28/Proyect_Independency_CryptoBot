"""Posiciones abiertas y entradas pendientes de aprobación (Paso 10c: aprobar y rechazar con confirmación)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.services import commands, views
from app.ui.components import format as f
from app.ui.components.confirm_action import confirm_action, show_last_action_result
from app.ui.components.indicator_tooltip import render_indicator_label
from app.ui.components.read_only import motivo_solo_lectura


def render() -> None:
    show_last_action_result()
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
    solo_lectura = motivo_solo_lectura() if v.entradas_pendientes else None
    bloqueo = (
        solo_lectura if solo_lectura
        else "Existe el archivo PARAR: no se pueden aprobar entradas nuevas." if v.parada_emergencia
        else "El sistema está pausado por drawdown: no se pueden aprobar entradas nuevas." if v.sistema_pausado
        else None
    )
    for e in v.entradas_pendientes:
        with st.container(border=True):
            st.markdown(f"**{e.symbol}** · {f.usd(e.requested_usd)} · vence {f.fecha(e.expires_at)}")
            st.caption(f"Precio de referencia del análisis: {f.precio(e.reference_price)} · creada {f.fecha(e.created_at)}")
            a, b = st.columns(2)
            with a:
                confirm_action(
                    f"aprobar-{e.order_id}", "Aprobar entrada",
                    f"Vas a ABRIR una posición paper de {f.usd(e.requested_usd)} en {e.symbol}. Se ejecuta al precio de "
                    "Binance de este momento (no al de referencia), con stop -8 % y objetivo +12 % sobre ese precio. "
                    "No hay dinero real.",
                    lambda _motivo, oid=e.order_id: commands.aprobar_entrada(oid),
                    disabled_reason=bloqueo,
                )
            with b:
                confirm_action(
                    f"rechazar-{e.order_id}", "Rechazar entrada",
                    f"Vas a RECHAZAR la entrada en {e.symbol}. Se liberan los cupos que reservaba y no se puede deshacer.",
                    lambda motivo, oid=e.order_id: commands.rechazar_entrada(oid, motivo),
                    ask_reason=True,
                    disabled_reason=solo_lectura,
                )
