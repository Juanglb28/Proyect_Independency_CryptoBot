"""Etiquetas de indicadores/métricas con su definición (sección 8.1).

El texto del tooltip sale del catálogo versionado (`app.ui.definitions`); si la
clave no existe no se muestra tooltip en vez de inventar uno. Un tooltip solo
explica: no calcula ni cambia nada.
"""

from __future__ import annotations

import streamlit as st

from app.ui.definitions import DEFINITION_CATALOG, DefinitionCatalog


def render_indicator_label(
    key: str, value: str, definition_catalog: DefinitionCatalog = DEFINITION_CATALOG, *, label: str | None = None
) -> None:
    """Muestra `value` (ya formateado) con el nombre y el tooltip de `key`."""
    definicion = definition_catalog.get(key)
    nombre = label or (definicion.display_name if definicion else key)
    st.metric(nombre, value, help=definition_catalog.tooltip_text(key))
