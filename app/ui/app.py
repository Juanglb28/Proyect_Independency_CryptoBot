"""Punto de entrada de la interfaz local (Paso 10).

    streamlit run app/ui/app.py

Solo navegación y presentación: las páginas leen con `app.services.views` y las
únicas acciones (aprobar/rechazar una entrada, reanudar) pasan por
`app.services.commands` con confirmación; no hay SQL ni cálculos financieros.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `streamlit run` pone la carpeta de este archivo al principio de `sys.path`;
# como aquí hay un `app.py`, `import app` resolvería a ESTE archivo y no al
# paquete `app`. Se retira esa carpeta y se asegura la raíz del repositorio
# para que los imports del proyecto (`app.services`, `app.ui...`) funcionen
# también cuando el paquete no está instalado en el entorno.
_AQUI = Path(__file__).resolve().parent
_RAIZ = str(_AQUI.parents[1])
sys.path[:] = [p for p in sys.path if p != str(_AQUI)]
if _RAIZ not in sys.path:
    sys.path.insert(0, _RAIZ)

import streamlit as st  # noqa: E402

from app.ui.definitions import CATALOG_VERSION  # noqa: E402
from app.ui.pages import analisis, dashboard, decisiones, operaciones, performance, posiciones, sistema, validacion  # noqa: E402

PAGINAS = (
    ("Dashboard", "dashboard", dashboard.render),
    ("Análisis", "analisis", analisis.render),
    ("Posiciones", "posiciones", posiciones.render),
    ("Operaciones", "operaciones", operaciones.render),
    ("Performance", "performance", performance.render),
    ("Validación de estrategia", "validacion", validacion.render),
    ("Decisiones", "decisiones", decisiones.render),
    ("Sistema", "sistema", sistema.render),
)


def main() -> None:
    st.set_page_config(page_title="Plataforma de Trading", layout="wide")
    paginas = [st.Page(fn, title=titulo, url_path=ruta, default=(ruta == "dashboard")) for titulo, ruta, fn in PAGINAS]
    with st.sidebar:
        st.caption("Paper trading · sin dinero real")
        st.caption(f"Definiciones de indicadores v{CATALOG_VERSION}")
    st.navigation(paginas).run()


main()
