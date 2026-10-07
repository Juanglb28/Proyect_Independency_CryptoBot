"""Análisis por factor (sección 8.8): peso asignado junto a lo observado.

Solo presentación: todos los números vienen calculados de `analyze_factors`.
Un dato ausente se muestra como `n/d`; nunca se rellena.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.ui.components import format as f

_NOMBRES = {
    "tendencia_momentum": "Tendencia y momentum",
    "volumen_liquidez": "Volumen y liquidez",
    "posicion_rangos_ath": "Posición en rangos / ATH",
    "contexto_eventos": "Contexto de eventos",
}


def _confianza(c) -> str:
    texto = f"alta {c.alta} · media {c.media} · baja {c.baja}"
    return texto + (f" · sin dato {c.sin_dato}" if c.sin_dato else "")


def _ic(a) -> str:
    return f"[{f.num(a.ci_lower)}, {f.num(a.ci_upper)}]" if a.ci_lower is not None else f.ND


def render_factor_analysis(informe) -> None:
    st.caption(
        f"{informe.n_con_propuesta_y_factores} de {informe.n_operaciones} operaciones cerradas tienen factores "
        f"guardados. Contribución = expectancy del tercio alto menos la del bajo del puntaje del factor; IC "
        f"{informe.confidence_level_pct}% por bootstrap ({informe.iterations} iteraciones, semilla {informe.seed})."
    )
    st.dataframe(
        pd.DataFrame(
            {
                "Factor": [_NOMBRES.get(a.factor, a.factor) for a in informe.factores],
                "Peso asignado": [f.num(a.peso) for a in informe.factores],
                "Operaciones": [a.n_operaciones for a in informe.factores],
                "% del total": [f.pct(a.pct_de_operaciones) for a in informe.factores],
                "Resultado promedio (USD)": [f.num(a.resultado_promedio_usd) for a in informe.factores],
                "Win rate": [f.pct(a.win_rate_pct) for a in informe.factores],
                "Contribución (USD)": [f.num(a.contribucion_usd) for a in informe.factores],
                "IC 95%": [_ic(a) for a in informe.factores],
                "Discrimina": [f.si_no(a.discrimina) for a in informe.factores],
                "Confianza (etiquetas)": [_confianza(a.confianza) for a in informe.factores],
                "Estado de la muestra": [a.estado.value for a in informe.factores],
            }
        ),
        hide_index=True,
    )
    st.caption(
        "Son 4 factores con un IC 95% cada uno: por azar alguno puede salir «discrimina» aunque no tenga relación "
        "real (comparaciones múltiples; no se corrige). La lectura —¿los factores con más peso se relacionan con "
        "el resultado?— es tuya; el sistema no la califica."
    )
    for nota in informe.notas:
        st.caption(f"Nota: {nota}")
    for a in informe.factores:
        for nota in a.notas:
            st.caption(f"{_NOMBRES.get(a.factor, a.factor)}: {nota}")
