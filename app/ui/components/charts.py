"""Gráficos de series ya preparadas por los servicios.

Aquí solo se convierte a números de gráfico (`float`) para DIBUJAR; ningún
valor mostrado como cifra sale de esta conversión.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st


def _frame(series: dict[str, tuple]) -> pd.DataFrame:
    filas = [
        {"hora": p.time, "valor": float(p.value), "serie": nombre}
        for nombre, puntos in series.items()
        for p in puntos
    ]
    return pd.DataFrame(filas, columns=["hora", "valor", "serie"])


def equity_curve(equity, benchmark_btc=()) -> None:
    """Equity del sistema y, si hay datos, BTC reescalado al capital inicial."""
    if len(equity) < 2:
        st.caption("La curva de equity aparecerá cuando el monitor tenga al menos dos observaciones.")
        return
    series = {"Equity del sistema (USD)": equity}
    if benchmark_btc:
        series["BTC comprado y mantenido (USD, mismo capital inicial)"] = benchmark_btc
    st.line_chart(_frame(series), x="hora", y="valor", color="serie")


def drawdown_chart(drawdown) -> None:
    if len(drawdown) < 2:
        st.caption("El drawdown aparecerá cuando el monitor tenga al menos dos observaciones.")
        return
    st.line_chart(_frame({"Drawdown (%)": drawdown}), x="hora", y="valor", color="serie")
