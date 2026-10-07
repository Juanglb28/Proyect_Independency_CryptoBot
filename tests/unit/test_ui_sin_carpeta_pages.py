"""Regresión: no debe existir una carpeta `pages/` junto a `app/ui/app.py`.

Streamlit la descubre sola y, al recargar o abrir una URL como `/posiciones`,
ejecuta ese archivo suelto en vez del punto de entrada (pantalla en blanco).
Bug real visto en el navegador el 2026-10-06; ver `app/ui/paginas/__init__.py`.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "app" / "ui"


def test_no_hay_carpeta_pages_junto_al_punto_de_entrada():
    assert (UI / "app.py").exists()
    assert not (UI / "pages").exists(), "renombra a `paginas/`: Streamlit auto-descubre `pages/`"


def test_las_paginas_viven_en_paginas():
    assert (UI / "paginas" / "dashboard.py").exists()
