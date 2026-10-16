"""Operaciones cerradas: listado con filtros y detalle (solo lectura)."""

from __future__ import annotations

from decimal import Decimal

import streamlit as st

from app.domain.enums import EstadoTesis, IntencionOrden, ResultadoOperacion, SerieOperacion
from app.services import views
from app.services.trades import FiltroOperaciones
from app.ui.components.trade_detail import render_trade_detail
from app.ui.components.trade_table import render_trade_table

_TODOS = "Todos"


def _elegir(etiqueta: str, enum_cls):
    opcion = st.selectbox(etiqueta, [_TODOS, *[e.value for e in enum_cls]])
    return None if opcion == _TODOS else enum_cls(opcion)


def render() -> None:
    st.title("Operaciones")
    st.caption("Operaciones cerradas (fuente: resultados definitivos). Más reciente primero. "
               "Las de OBSERVACIÓN (candidatos del pre-filtro abiertos sin IA) se distinguen de las de la ESTRATEGIA y se miden aparte.")
    todas = views.operaciones()
    simbolos = sorted({x.symbol for x in todas})

    c1, c2, c3 = st.columns(3)
    with c1:
        simbolo = st.selectbox("Símbolo", [_TODOS, *simbolos])
        resultado = _elegir("Resultado", ResultadoOperacion)
        serie = _elegir("Serie", SerieOperacion)
    with c2:
        motivo = _elegir("Motivo de salida", IntencionOrden)
        tesis = _elegir("Estado de la tesis", EstadoTesis)
    with c3:
        score_min = st.number_input("Score mínimo", min_value=0, max_value=100, value=0)
        score_max = st.number_input("Score máximo", min_value=0, max_value=100, value=100)

    filtro = FiltroOperaciones(
        symbol=None if simbolo == _TODOS else simbolo,
        resultado=resultado, exit_reason=motivo, thesis_status=tesis, serie=serie,
        score_min=Decimal(score_min) if score_min > 0 else None,
        score_max=Decimal(score_max) if score_max < 100 else None,
    )
    filas = views.operaciones(filtro)
    render_trade_table(filas)

    if filas:
        st.divider()
        etiquetas = {f"{x.exit_time:%Y-%m-%d %H:%M} · {x.symbol} · {x.net_pnl:.2f} USD": x.position_id for x in filas}
        elegida = st.selectbox("Ver el detalle de una operación", list(etiquetas))
        detalle = views.operacion_detalle(etiquetas[elegida])
        if detalle is not None:
            render_trade_detail(detalle)
