"""Detalle de una operación cerrada: decisión congelada, trayectoria y resultado."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.ui.components import format as f
from app.ui.components.indicator_tooltip import render_indicator_label
from app.ui.components.thesis_result import render_thesis_result


def render_trade_detail(detalle) -> None:
    fila, r, decision = detalle.fila, detalle.resultado, detalle.decision
    st.subheader(f"{fila.symbol} · {fila.resultado.value}")
    a, b, c, d = st.columns(4)
    a.metric("P&L neto", f.usd(r.net_pnl), help=f"Bruto {f.usd(r.gross_pnl)} · comisiones {f.usd(r.total_fees)}")
    b.metric("Retorno neto", f.pct(r.net_return_pct))
    with c:
        render_indicator_label("r_multiple", f.num(r.realized_r_multiple), label="R realizado")
    d.metric("Duración", f"{r.duration_seconds // 3600} h {r.duration_seconds % 3600 // 60} min")
    e, g, _, _ = st.columns(4)
    with e:
        render_indicator_label("mae", f.pct(r.mae_pct))
    with g:
        render_indicator_label("mfe", f.pct(r.mfe_pct))

    st.markdown("#### Tesis y resultado")
    render_thesis_result(decision, fila)

    st.markdown("#### Lo que el sistema sabía al decidir")
    if decision is None:
        st.caption("Sin propuesta asociada.")
    else:
        h, i, j = st.columns(3)
        with h:
            render_indicator_label("opportunity_score", f.num(decision.puntaje_ponderado), label="Score ponderado")
        i.metric("Puntaje base", f.num(decision.puntaje_base),
                 help=f"Calidad de datos aplicada: {f.num(decision.calidad_datos)}")
        j.metric("Confianza global", decision.confianza_global)
        if decision.factores:
            st.dataframe(
                pd.DataFrame(
                    {"Factor": [x.factor for x in decision.factores],
                     "Peso": [f.num(x.peso) for x in decision.factores],
                     "Puntaje": [f.num(x.puntaje) for x in decision.factores],
                     "Confianza": [x.confianza or f.ND for x in decision.factores]}
                ),
                hide_index=True,
            )
        with st.expander("Decision snapshot congelado (JSON exacto)"):
            st.json(decision.decision_snapshot)

    st.markdown("#### Trayectoria")
    if len(detalle.trayectoria) < 2:
        st.caption("No hay suficientes observaciones guardadas para dibujar la trayectoria.")
    else:
        st.line_chart(
            pd.DataFrame(
                {"hora": [p.time for p in detalle.trayectoria],
                 "Precio": [float(p.market_price) for p in detalle.trayectoria]}
            ),
            x="hora", y="Precio",
        )
