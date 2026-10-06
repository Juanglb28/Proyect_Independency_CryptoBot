"""Tabla de operaciones cerradas."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.ui.components import format as f


def render_trade_table(filas) -> None:
    if not filas:
        st.info("No hay operaciones cerradas que cumplan el filtro.")
        return
    st.dataframe(
        pd.DataFrame(
            {
                "Cierre": [f.fecha(x.exit_time) for x in filas],
                "Símbolo": [x.symbol for x in filas],
                "Score": [f.num(x.score) for x in filas],
                "Entrada": [f.precio(x.entry_price) for x in filas],
                "Salida": [f.precio(x.exit_price) for x in filas],
                "Retorno neto": [f.pct(x.net_return_pct) for x in filas],
                "P&L neto (USD)": [f.num(x.net_pnl) for x in filas],
                "R": [f.num(x.r_multiple) for x in filas],
                "MAE": [f.pct(x.mae_pct) for x in filas],
                "MFE": [f.pct(x.mfe_pct) for x in filas],
                "Resultado": [x.resultado.value for x in filas],
                "Tesis": [x.thesis_status.value for x in filas],
                "Motivo de salida": [x.exit_reason.value for x in filas],
            }
        ),
        hide_index=True,
       
    )
