"""Dashboard: vista ejecutiva (solo lectura)."""

from __future__ import annotations

import streamlit as st

from app.services import views
from app.ui.components import format as f
from app.ui.components.benchmark import render_benchmark
from app.ui.components.charts import drawdown_chart, equity_curve
from app.ui.components.indicator_tooltip import render_indicator_label
from app.ui.components.metrics_cards import render_metric_cards


def render() -> None:
    v = views.dashboard()
    st.title("Dashboard")
    st.caption("Paper trading: no hay dinero real. Datos al " + f.fecha(v.generado_en))
    if v.parada_emergencia:
        st.error("PARADA DE EMERGENCIA activa: el sistema no abre ni gestiona operaciones.")
    elif v.sistema_pausado:
        st.warning("Sistema PAUSADO por drawdown: no se abren entradas nuevas (las posiciones abiertas siguen vigiladas).")

    a, b, c, d = st.columns(4)
    a.metric("Capital inicial", f.usd(v.capital_inicial))
    b.metric("Equity actual", f.usd(v.equity_actual),
             help="Última observación del monitor." if v.equity_actual is not None else "El monitor aún no ha observado.")
    with c:
        render_indicator_label("max_drawdown", f.pct(v.drawdown_actual_pct), label="Drawdown actual")
    d.metric("Posiciones abiertas", v.posiciones_abiertas)
    st.caption(f"Pico de equity: {f.usd(v.pico_equity)} · última observación: {f.fecha(v.ultima_observacion_en)}")

    st.subheader("Rendimiento del historial completo")
    render_metric_cards(v.metricas)
    st.subheader("Equity frente a BTC")
    equity_curve(v.curva_equity, v.curva_benchmark_btc)
    st.subheader("Drawdown")
    drawdown_chart(v.curva_drawdown)
    st.subheader("Benchmark")
    render_benchmark(v.metricas)
