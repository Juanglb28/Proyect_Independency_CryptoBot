"""Análisis del score por tercil (¿el score alto rinde mejor que el bajo?)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.ui.components import format as f


def render_tercile_table(terciles) -> None:
    if not terciles:
        st.caption("Todavía no hay terciles: hacen falta al menos 3 operaciones con score.")
        return
    st.dataframe(
        pd.DataFrame(
            {"Tercil": [m.scope_key for m in terciles],
             "Operaciones": [m.total_trades for m in terciles],
             "Win rate": [f.pct(m.win_rate_pct) for m in terciles],
             "Expectancy (USD)": [f.num(m.expectancy_per_trade) for m in terciles],
             "P&L neto (USD)": [f.num(m.net_pnl) for m in terciles],
             "Score medio": [f.num(m.average_opportunity_score) for m in terciles]}
        ),
        hide_index=True,
    )


def render_validation_report(informe) -> None:
    st.markdown(f"**Estado de la muestra:** `{informe.status.value}` · {informe.n_trades} operaciones "
                f"({informe.n_trades_con_score} con score; el plan pide {informe.min_trades_plan} para concluir)")
    if informe.tercil_size is None:
        st.info("Muestra insuficiente: aún no se pueden formar el tercio superior e inferior.")
    else:
        a, b, c = st.columns(3)
        a.metric("Expectancy tercio alto", f.usd(informe.high_score_expectancy))
        b.metric("Expectancy tercio bajo", f.usd(informe.low_score_expectancy))
        c.metric("Discriminación", f.usd(informe.score_discrimination),
                 help="Expectancy del tercio alto menos la del bajo.")
        st.markdown(
            f"IC {informe.confidence_level_pct}% por bootstrap ({informe.iterations} iteraciones, semilla "
            f"{informe.seed}): [{f.num(informe.ci_lower)}, {f.num(informe.ci_upper)}] · "
            f"discrimina (límite inferior > 0): **{f.si_no(informe.discriminates)}**"
        )
        if informe.status.value == "PRELIMINAR":
            st.warning("Muestra preliminar: el resultado es solo orientativo.")
    for nota in informe.notas:
        st.caption(f"Nota: {nota}")


def render_stratified_report(est) -> None:
    """Medición adicional de la enmienda E4: resultados centrados por mes de entrada."""
    informe = est.informe
    st.caption(
        "Misma comparación de terciles, pero cada resultado se mide contra el promedio de su mes de entrada, "
        "para separar la capacidad del score del régimen de mercado (enmienda E4). Es una medición adicional: "
        "no sustituye a la principal."
    )
    st.markdown(f"**Estado de la muestra:** `{informe.status.value}` · {informe.n_trades_con_score} operaciones usadas · "
                f"{est.estratos_usados} meses usados, {est.estratos_excluidos} excluidos")
    if informe.tercil_size is None:
        st.info("Muestra insuficiente para formar terciles dentro de los meses.")
    else:
        a, b, c = st.columns(3)
        a.metric("Desviación tercio alto", f.usd(informe.high_score_expectancy))
        b.metric("Desviación tercio bajo", f.usd(informe.low_score_expectancy))
        c.metric("Discriminación", f.usd(informe.score_discrimination))
        st.markdown(f"IC {informe.confidence_level_pct}%: [{f.num(informe.ci_lower)}, {f.num(informe.ci_upper)}] · "
                    f"discrimina (límite inferior > 0): **{f.si_no(informe.discriminates)}**")
        if informe.status.value == "PRELIMINAR":
            st.warning("Muestra preliminar: el resultado es solo orientativo.")
    if est.alerta_de_regimen:
        st.warning("La medición principal dice que discrimina y la estratificada no: posible efecto de régimen de mercado.")
    for nota in informe.notas:
        st.caption(f"Nota: {nota}")
