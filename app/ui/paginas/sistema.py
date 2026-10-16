"""Sistema: salud de la base, datos, monitor, scheduler, reconciliación y costo de IA; reanudar tras una pausa (con confirmación)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.services import commands, views
from app.ui.components import format as f
from app.ui.components.confirm_action import confirm_action, show_last_action_result
from app.ui.components.read_only import motivo_solo_lectura


def render() -> None:
    show_last_action_result()
    v = views.sistema()
    st.title("Sistema")
    st.caption("Datos al " + f.fecha(v.generado_en))

    (st.success if v.base_de_datos_ok and v.timescaledb_ok else st.error)(v.detalle_base_de_datos)
    a, b, c = st.columns(3)
    a.metric("Estado", "PARADA DE EMERGENCIA" if v.parada_emergencia else ("PAUSADO" if v.sistema_pausado else "Activo"))
    b.metric("Posiciones abiertas", v.posiciones_abiertas)
    c.metric("Último dato del monitor", f.duracion(v.edad_monitor), help=f.fecha(v.ultima_observacion_monitor))

    # ── Scheduler / Heartbeat ──
    hb = v.heartbeat
    st.subheader("Scheduler")
    if not hb.disponible:
        st.warning("Heartbeat no disponible: el scheduler no se ha ejecutado o el archivo no es accesible.")
    elif hb.stale:
        st.error(
            f"Heartbeat VIEJO: última señal hace {f.duracion(hb.edad)} "
            f"({f.fecha(hb.timestamp)}). El scheduler puede estar caído."
        )
    else:
        st.success(f"Scheduler activo: última señal hace {f.duracion(hb.edad)} (PID {hb.pid})")

    if v.sistema_pausado:
        st.subheader("Reanudar tras la pausa por drawdown")
        st.caption(f"Equity: {f.usd(v.equity_actual)} · pico vigente: {f.usd(v.pico_equity)}")
        confirm_action(
            "reanudar", "Reanudar el sistema",
            "Vas a REANUDAR el sistema. El pico de equity se reinicia al equity de este momento (el pico anterior "
            f"queda registrado; hoy es {f.usd(v.pico_equity)}). Hace falta el precio de todas las posiciones abiertas; "
            "si falta alguno no se reanuda. Si el archivo PARAR existe, las entradas nuevas siguen bloqueadas.",
            lambda _motivo: commands.reanudar_sistema(),
            disabled_reason=motivo_solo_lectura(),
        )

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
    if v.presupuesto_ia is not None:
        pr = v.presupuesto_ia
        st.metric("Gasto de IA hoy, UTC (USD)", f.num(pr.gasto_hoy_usd, 4),
                  help=f"Tope diario de los ciclos automáticos: {f.num(pr.tope_usd)} USD. "
                       "Al alcanzarlo no se inician más ciclos con IA hasta las 00:00 UTC; "
                       "el monitor y los stops no usan IA y siguen funcionando.")
        if pr.agotado:
            st.warning("Tope diario de IA alcanzado: no se inician más ciclos automáticos con IA hoy.")
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
