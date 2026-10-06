"""Performance: métricas por periodo, símbolo y tercil de score (solo lectura)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.services import views
from app.ui.components import format as f
from app.ui.components.benchmark import render_benchmark
from app.ui.components.metrics_cards import render_metric_cards
from app.ui.components.score_analysis import render_tercile_table

_PERIODOS = {"Todo el historial": None, "Últimos 7 días": 7, "Últimos 30 días": 30, "Últimos 90 días": 90}


def render() -> None:
    st.title("Performance")
    periodo = st.selectbox("Periodo", list(_PERIODOS))
    v = views.performance(days=_PERIODOS[periodo])
    st.caption(f"Del {f.fecha(v.periodo_inicio)} al {f.fecha(v.periodo_fin)}. Se calcula al abrir la página con "
               "las mismas funciones que `scripts.results performance`; no escribe en la base.")
    render_metric_cards(v.global_)
    st.subheader("Contra el benchmark")
    render_benchmark(v.global_)
    st.subheader("Por símbolo")
    if not v.por_simbolo:
        st.caption("Sin operaciones en el periodo.")
    else:
        st.dataframe(
            pd.DataFrame({"Símbolo": [m.scope_key for m in v.por_simbolo],
                          "Operaciones": [m.total_trades for m in v.por_simbolo],
                          "Win rate": [f.pct(m.win_rate_pct) for m in v.por_simbolo],
                          "P&L neto (USD)": [f.num(m.net_pnl) for m in v.por_simbolo],
                          "Expectancy (USD)": [f.num(m.expectancy_per_trade) for m in v.por_simbolo]}),
            hide_index=True,
        )
    st.subheader("Por tercil de score")
    render_tercile_table(v.por_tercil)
