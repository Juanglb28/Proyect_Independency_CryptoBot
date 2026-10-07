"""Páginas de la interfaz. Cada módulo expone `render()` y solo muestra lo que
entrega `app.services.views`.

POR QUÉ ESTA CARPETA SE LLAMA `paginas` Y NO `pages` (como en la sección 7 del
master prompt): Streamlit trata cualquier carpeta `pages/` junto al archivo de
entrada (`app/ui/app.py`) como su sistema de páginas automático. Con ella,
recargar o abrir directamente `/posiciones` ejecuta `pages/posiciones.py` como
si fuera el programa principal, sin pasar por `app.py` (pantalla en blanco o
`ModuleNotFoundError`), y el menú lateral muestra los nombres de archivo en
minúsculas. La navegación real está en `app/ui/app.py` (`st.navigation`).
Un test (`tests/unit/test_ui_sin_carpeta_pages.py`) impide que vuelva a existir.
"""
