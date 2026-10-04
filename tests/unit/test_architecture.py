"""Reglas de dependencias entre capas (master prompt, secciones 8, 22 y 35).

Se leen los `import` de cada módulo con `ast` — sin ejecutarlos — y se
comprueba que ninguna capa importe lo que tiene prohibido. Si este test
falla, no se "arregla" el test: se corrige la dependencia o se discute la
regla (cambiar la arquitectura exige comunicarlo, restricción 14).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[2] / "app"

# capa -> prefijos de módulo que NO puede importar
_PROHIBIDO: dict[str, tuple[str, ...]] = {
    # El dominio son reglas puras: sin infraestructura, IA, UI ni persistencia.
    "domain": (
        "sqlalchemy", "streamlit", "ccxt", "anthropic", "psycopg", "alembic",
        "app.db", "app.repositories", "app.data", "app.ai", "app.execution",
        "app.results", "app.orchestration", "app.ui",
    ),
    # Los agentes de IA nunca están en el camino de una orden (P2/P4).
    "execution": ("anthropic", "app.ai", "streamlit", "app.ui"),
    # Los repositorios solo persisten: ni IA, ni UI, ni reglas de negocio.
    "repositories": ("anthropic", "app.ai", "streamlit", "app.ui", "app.domain.rules", "app.execution"),
    # La UI no toca SQL ni la base directamente (UI -> servicios -> repositorios).
    "ui": ("sqlalchemy", "psycopg", "app.db", "app.repositories", "anthropic"),
    # La capa de datos no ejecuta órdenes ni depende de la ejecución.
    "data": ("app.execution", "streamlit", "app.ui"),
}


def _imports(path: Path) -> set[str]:
    modulos: set[str] = set()
    for nodo in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(nodo, ast.Import):
            modulos.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.level == 0:
            modulos.add(nodo.module)
    return modulos


def _violaciones(capa: str) -> list[str]:
    encontradas = []
    for archivo in sorted((_APP / capa).rglob("*.py")):
        for modulo in sorted(_imports(archivo)):
            for prohibido in _PROHIBIDO[capa]:
                if modulo == prohibido or modulo.startswith(prohibido + "."):
                    encontradas.append(f"{archivo.relative_to(_APP.parent)} importa {modulo}")
    return encontradas


@pytest.mark.parametrize("capa", sorted(_PROHIBIDO))
def test_ninguna_capa_importa_lo_que_tiene_prohibido(capa):
    if not (_APP / capa).is_dir():
        pytest.skip(f"app/{capa} no existe todavía")
    assert _violaciones(capa) == []


def test_la_deteccion_funciona(tmp_path):
    """Comprobación del propio detector: un import prohibido SE detecta."""
    archivo = tmp_path / "malo.py"
    archivo.write_text("from app.ai.agents.auditor import run_auditor_datos\nimport anthropic\n")
    assert _imports(archivo) == {"app.ai.agents.auditor", "anthropic"}
