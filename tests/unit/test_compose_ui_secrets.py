"""El servicio `ui` de docker-compose.yml no debe recibir secretos que no usa
(P4: privilegios mínimos). Prueba estática sobre el texto del archivo, sin
Docker ni dependencias de YAML.

Qué protege: la interfaz solo necesita la base de datos, la clave de
Anthropic (Entrevistador) y valores no secretos. Las claves de Binance y el
token de Telegram nunca deben llegar a ese proceso, ni por `env_file`, ni
por `environment`, ni leyendo `/app/.env` a través del volumen del repo.
"""

from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
COMPOSE = (RAIZ / "docker-compose.yml").read_text(encoding="utf-8")


def _servicio(nombre: str) -> str:
    """Texto del servicio `nombre` (desde su línea hasta el siguiente servicio
    o sección de primer nivel)."""
    m = re.search(rf"^  {nombre}:\n(.*?)(?=^  \S|^\S)", COMPOSE, flags=re.S | re.M)
    assert m, f"no se encontró el servicio {nombre}"
    return m.group(1)


def _sin_comentarios(texto: str) -> str:
    return "\n".join(re.sub(r"\s#.*$", "", l) for l in texto.splitlines() if not l.lstrip().startswith("#"))


def test_ui_no_usa_env_file():
    assert "env_file" not in _sin_comentarios(_servicio("ui"))


def test_ui_no_declara_claves_de_binance_ni_telegram():
    ui = _sin_comentarios(_servicio("ui")).upper()
    for prohibida in ("BINANCE", "TELEGRAM"):
        assert prohibida not in ui


def test_ui_solo_declara_las_variables_esperadas():
    ui = _sin_comentarios(_servicio("ui"))
    bloque = re.search(r"environment:\n((?:      .*\n)+)", ui)
    assert bloque, "ui debe declarar `environment` explícito"
    variables = {l.strip().split(":")[0] for l in bloque.group(1).splitlines() if l.strip()}
    assert variables == {
        "DATABASE_URL",
        "PAPER_CAPITAL_USD",
        "MAX_OPEN_POSITIONS",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_SALDO_DECLARADO_USD",
        "ANTHROPIC_SALDO_DECLARADO_EN",
    }


def test_ui_tapa_el_env_real_con_un_archivo_sin_secretos():
    assert "./docker/ui-sin-secretos.env:/app/.env:ro" in _servicio("ui")
    contenido = (RAIZ / "docker" / "ui-sin-secretos.env").read_text(encoding="utf-8")
    valores = [l for l in contenido.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert valores == [], "el archivo que tapa .env no debe contener variables"


def test_app_conserva_su_env_file():
    """`app` (scripts, monitor, ciclo diario) sí necesita todas las claves."""
    assert "env_file" in _servicio("app")


def test_ui_railway_es_de_solo_lectura_sin_secretos_y_solo_en_localhost():
    """Paso 11-0: la interfaz que apunta a Railway no recibe ningún secreto de
    escritura ni de terceros, solo la URL del rol de solo lectura."""
    s = _sin_comentarios(_servicio("ui-railway"))
    assert "profiles" in s and "railway" in s  # no arranca con un `up` normal
    assert "env_file" not in s
    for prohibida in ("BINANCE", "TELEGRAM", "ANTHROPIC"):
        assert prohibida not in s.upper()
    bloque = re.search(r"environment:\n((?:      .*\n)+)", s)
    variables = {l.strip().split(":")[0] for l in bloque.group(1).splitlines() if l.strip()}
    assert variables == {"DATABASE_URL", "PAPER_CAPITAL_USD", "MAX_OPEN_POSITIONS"}
    assert "UI_DATABASE_URL" in s
    assert "./docker/ui-sin-secretos.env:/app/.env:ro" in s
    assert '"127.0.0.1:8502:8501"' in s
