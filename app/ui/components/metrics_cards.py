"""Tarjetas con las métricas de rendimiento ya calculadas por los servicios."""

from __future__ import annotations

import streamlit as st

from app.ui.components import format as f
from app.ui.components.indicator_tooltip import render_indicator_label


def render_metric_cards(m) -> None:
    """`m` es una fila de métricas (`PerformanceMetrics`); se muestra tal cual."""
    if m.total_trades == 0:
        st.info("Todavía no hay operaciones cerradas en este periodo: las métricas aparecerán cuando las haya.")
    a, b, c, d = st.columns(4)
    a.metric("Operaciones", m.total_trades,
             help=f"Ganadoras {m.winning_trades} · perdedoras {m.losing_trades} · equilibrio {m.breakeven_trades}")
    b.metric("Win rate", f.pct(m.win_rate_pct))
    c.metric("P&L neto", f.usd(m.net_pnl), help=f"Bruto {f.usd(m.gross_pnl)} · comisiones {f.usd(m.total_fees)}")
    d.metric("Retorno total", f.pct(m.total_return_pct))
    e, g, h, i = st.columns(4)
    with e:
        render_indicator_label("expectancy", f.usd(m.expectancy_per_trade))
    with g:
        render_indicator_label("profit_factor", f.num(m.profit_factor))
    with h:
        render_indicator_label("max_drawdown", f.pct(m.max_drawdown_pct))
    with i:
        render_indicator_label("r_multiple", f.num(m.average_r_multiple), label="R medio")
    j, k, _, _ = st.columns(4)
    with j:
        render_indicator_label("mae", f.pct(m.average_mae_pct), label="MAE medio")
    with k:
        render_indicator_label("mfe", f.pct(m.average_mfe_pct), label="MFE medio")
