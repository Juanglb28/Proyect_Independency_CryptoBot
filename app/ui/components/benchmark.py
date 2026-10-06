"""Comparación con el benchmark (BTC comprado y mantenido)."""

from __future__ import annotations

import streamlit as st

from app.ui.components import format as f


def render_benchmark(m) -> None:
    sistema, btc = m.total_return_pct, m.benchmark_return_pct
    a, b, c = st.columns(3)
    a.metric("Retorno del sistema", f.pct(sistema))
    b.metric("Retorno de BTC (comprar y mantener)", f.pct(btc),
             help="Mismo periodo, con precios que el sistema observó (velas de 1 h), sin datos futuros.")
    c.metric("Drawdown de BTC en el periodo", f.pct(m.benchmark_max_drawdown_pct))
    if sistema is None or btc is None:
        st.caption("Sin datos suficientes para comparar contra el benchmark todavía.")
