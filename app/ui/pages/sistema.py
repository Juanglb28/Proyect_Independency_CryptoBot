"""Sistema: salud de la base, datos, monitor, reconciliación y costo de IA (solo lectura)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.services import views
from app.ui.components import format as f


def render() -> None:
    v = views.sistema()
    st.title("Sistema")
    st.caption("Datos al " + f.fecha(v.generado_en))

    (st.success if v.base_de_datos_ok and v.timescaledb_ok else st.error)(v.detalle_base_de_datos)
    a, b, c = st.columns(3)
    a.metric("Estado", "PARADA DE EMERGENCIA" if v.parada_emergencia else ("PAUSADO" if v.sistema_pausado else "Activo"))
    b.metric("Posiciones abiertas", v.posiciones_abiertas)
    c.metric("Último dato del monitor", f.duracion(v.edad_monitor), help=f.fecha(v.ultima_observacion_monitor))

    st.subheader("Reconciliación")
    if v.discrepancias:
        for d in v.discrepancias:
            st.error(d)
    else:
        st.success("Sin discrepancias entre órdenes y posiciones.")

    st.subheader("Frescura de los datos de mercado")
    st.dataframe(
        pd.DataFrame({"Símbolo": [x.symbol for x in v.velas], "Temporalidad": [x.timeframe for x in v.velas],
                      "Última vela": [f.fecha(x.ultima_vela) for x in v.velas],
                      "Antigüedad": [f.duracion(x.edad) for x in v.velas]}),
        hide_index=True,
    )

    st.subheader("Costo de IA")
    st.metric("Últimos 30 días (USD)", f.num(v.costo_ia.costo_usd, 4),
              help=f"{v.costo_ia.llamadas} llamadas, {v.costo_ia.fallidas} fallidas (se cobran igual).")
    if v.saldo_ia is None:
        st.caption("Saldo estimado no disponible: no hay saldo declarado en la configuración.")
    else:
        s = v.saldo_ia
        st.metric("Saldo estimado (USD)", f.num(s.saldo_estimado_usd),
                  help=f"Declarado {f.num(s.declarado_usd)} el {f.fecha(s.declarado_en)} menos gasto desde "
                       f"entonces {f.num(s.gasto_desde_entonces_usd)}. Es una estimación: la API no expone saldo.")

    st.subheader("Ciclos recientes")
    if not v.ciclos_recientes:
        st.caption("Sin ciclos todavía.")
    else:
        st.dataframe(
            pd.DataFrame({"Inicio": [f.fecha(c.started_at) for c in v.ciclos_recientes],
                          "Resultado": [c.resultado.value for c in v.ciclos_recientes],
                          "Propuestas": [c.n_propuestas for c in v.ciclos_recientes]}),
            hide_index=True,
        )
