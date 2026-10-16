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
        "app.results", "app.orchestration", "app.ui", "app.notifications",
    ),
    # Los agentes de IA nunca están en el camino de una orden (P2/P4).
    # Tampoco el envío de avisos (Paso 11a): el monitor solo deja el aviso en
    # la tabla `alertas`; el envío ocurre después, fuera de la ejecución.
    "execution": ("anthropic", "app.ai", "streamlit", "app.ui", "app.notifications"),
    # Los repositorios solo persisten: ni IA, ni UI, ni reglas de negocio.
    "repositories": (
        "anthropic", "app.ai", "streamlit", "app.ui", "app.domain.rules", "app.execution", "app.notifications",
    ),
    # Los avisos (Paso 11a): E/S externa (Telegram). Sin IA, sin UI, sin
    # ejecución y sin reglas de negocio: solo leen y anotan en `alertas`.
    "notifications": (
        "anthropic", "app.ai", "streamlit", "app.ui", "app.execution", "app.orchestration",
        "app.domain.rules", "app.results", "app.data", "ccxt",
    ),
    # La UI no toca SQL ni la base directamente (UI -> servicios -> repositorios).
    # Tampoco calcula ni ejecuta: solo muestra lo que entregan los servicios de
    # lectura (`app.services`). Puede usar los enums del dominio para filtros.
    "ui": (
        "sqlalchemy", "psycopg", "app.db", "app.repositories", "anthropic", "ccxt",
        "app.execution", "app.results", "app.orchestration", "app.data", "app.domain.rules",
        "app.notifications",
    ),
    # Servicios de lectura para la interfaz (Paso 10a): sin IA, sin UI. Leen
    # a través de repositorios y devuelven datos listos para mostrar; la
    # solo-lectura se garantiza además en la base (transacción READ ONLY).
    "services": ("anthropic", "app.ai", "streamlit", "app.ui", "app.orchestration", "app.notifications"),
    # Medición y validación: calculan y guardan resultados, sin IA, sin UI y
    # sin depender del motor de ejecución (la dependencia va al revés: el
    # monitor registra sus resultados a través de `results`).
    "results": ("anthropic", "app.ai", "streamlit", "app.ui", "app.execution", "app.notifications"),
    # La capa de datos no ejecuta órdenes ni depende de la ejecución.
    "data": ("app.execution", "streamlit", "app.ui", "app.notifications"),
    # Los agentes de IA no tienen acceso a la ejecución ni al exchange (P2/P4).
    "ai": ("app.execution", "ccxt", "app.data.exchange_client", "streamlit", "app.ui", "app.notifications"),
    # Experimentos (enmienda E6, Kronos): código de terceros que solo MIRA velas
    # y produce pronósticos para contrastar. Sin ejecución, sin exchange, sin
    # avisos, sin IA de agentes, sin UI, sin base: lo que necesita le llega por
    # argumento (el script `kronos_k0` abre la sesión de solo-lectura y se la da).
    "experiments": (
        "app.execution", "app.orchestration", "app.notifications", "app.ai", "app.ui", "app.services",
        "app.results", "app.db", "app.repositories", "app.config", "anthropic", "ccxt", "streamlit",
        "sqlalchemy", "psycopg",
    ),
}


def _imports(path: Path) -> set[str]:
    modulos: set[str] = set()
    for nodo in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(nodo, ast.Import):
            modulos.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.level == 0:
            modulos.add(nodo.module)
    return modulos


# Excepciones explícitas y mínimas: módulos de `app.execution` que los
# servicios de lectura pueden importar porque SOLO consultan (estado del
# sistema, reconciliación interna y el archivo PARAR). Cualquier otro módulo
# de ejecución (órdenes, posiciones, monitor) sigue prohibido.
_EXCEPCIONES: dict[str, tuple[str, ...]] = {
    "services": (
        "app.execution.system_control", "app.execution.reconciliation", "app.execution.emergency_stop",
    ),
}
_PROHIBIDO["services"] = _PROHIBIDO["services"] + ("app.execution",)

# Paso 10c: `app/services/commands.py` es el ÚNICO módulo de servicios que
# escribe (aprobar/rechazar entradas, reanudar) y por eso el único que puede
# importar estos módulos de ejecución. Es una excepción por ARCHIVO, no por capa.
_ARCHIVO_DE_ACCIONES = "services/commands.py"
# Paso 10b: `services/interview.py` es el ÚNICO servicio que llama a un agente
# de IA (el Entrevistador, informativo y sin herramientas).
_ARCHIVO_DE_ENTREVISTA = "services/interview.py"
_EXCEPCIONES_POR_ARCHIVO: dict[str, tuple[str, ...]] = {
    _ARCHIVO_DE_ACCIONES: (
        "app.execution.order_manager", "app.execution.price_source", "app.execution.system_control",
    ),
    _ARCHIVO_DE_ENTREVISTA: ("app.ai.agents.interviewer",),
}


def _violaciones(capa: str) -> list[str]:
    encontradas = []
    for archivo in sorted((_APP / capa).rglob("*.py")):
        por_archivo = _EXCEPCIONES_POR_ARCHIVO.get(archivo.relative_to(_APP).as_posix(), ())
        for modulo in sorted(_imports(archivo)):
            if any(modulo == e or modulo.startswith(e + ".") for e in (*_EXCEPCIONES.get(capa, ()), *por_archivo)):
                continue
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


def test_streamlit_solo_se_importa_dentro_de_la_interfaz():
    """Streamlit es una dependencia de presentación: ninguna otra capa la usa."""
    fuera = []
    for archivo in sorted(_APP.rglob("*.py")):
        if archivo.relative_to(_APP).parts[0] == "ui":
            continue
        if any(m == "streamlit" or m.startswith("streamlit.") for m in _imports(archivo)):
            fuera.append(str(archivo.relative_to(_APP.parent)))
    assert fuera == []


def test_los_servicios_solo_importan_los_modulos_de_ejecucion_permitidos():
    """Las excepciones no se amplían sin querer: solo existen las tres declaradas."""
    assert _EXCEPCIONES["services"] == (
        "app.execution.system_control", "app.execution.reconciliation", "app.execution.emergency_stop",
    )


def test_solo_la_interfaz_importa_las_acciones_y_las_lecturas_no():
    """`commands` (escribe) lo usa únicamente la interfaz. Ningún servicio de
    lectura —ni la fachada `views`— lo importa: la solo-lectura de las vistas
    depende de esa separación."""
    importadores = []
    for archivo in sorted(_APP.rglob("*.py")):
        relativo = archivo.relative_to(_APP).as_posix()
        if relativo == _ARCHIVO_DE_ACCIONES:
            continue
        if any(m == "app.services.commands" or m.startswith("app.services.commands.") for m in _imports(archivo)):
            importadores.append(relativo)
        if "from app.services import" in archivo.read_text(encoding="utf-8") and relativo.startswith("services/"):
            lineas = [x for x in archivo.read_text(encoding="utf-8").splitlines() if x.startswith("from app.services import")]
            if any("commands" in x for x in lineas):
                importadores.append(relativo)
    assert all(x.startswith("ui/") for x in importadores), importadores


def test_las_excepciones_por_archivo_no_se_amplian_sin_querer():
    assert _EXCEPCIONES_POR_ARCHIVO == {
        "services/commands.py": (
            "app.execution.order_manager", "app.execution.price_source", "app.execution.system_control",
        ),
        "services/interview.py": ("app.ai.agents.interviewer",),
    }


def _importadores(prefijo: str) -> set[str]:
    return {
        archivo.relative_to(_APP).as_posix()
        for archivo in sorted(_APP.rglob("*.py"))
        if any(m == prefijo or m.startswith(prefijo + ".") for m in _imports(archivo))
    }


def test_solo_la_interfaz_importa_la_entrevista():
    assert all(x.startswith("ui/") for x in _importadores("app.services.interview")), _importadores("app.services.interview")


def test_el_perfil_es_informativo_nada_de_decision_lo_lee():
    """Decisión del usuario (2026-10-06): el perfil no tiene efecto en agentes,
    score ni reglas. Solo el servicio que lo guarda y el que lo muestra pueden
    tocar su repositorio o su tabla."""
    permitidos = {
        "services/interview.py", "services/profile.py", "repositories/perfil_usuario.py",
        "db/models/__init__.py", "db/models/perfil_usuario.py",
    }
    for prefijo in ("app.repositories.perfil_usuario", "app.db.models.perfil_usuario"):
        assert _importadores(prefijo) <= permitidos, (prefijo, _importadores(prefijo) - permitidos)
    assert _importadores("app.services.profile") <= {"services/views.py"}


def test_el_entrevistador_no_toca_ejecucion_ni_exchange():
    """P4: el agente que recibe texto libre del usuario no importa nada de ejecución/datos de mercado."""
    modulos = _imports(_APP / "ai" / "agents" / "interviewer.py")
    assert not [m for m in modulos if m.startswith(("app.execution", "app.data", "ccxt"))], modulos


def test_la_interfaz_y_los_servicios_no_usan_credenciales_del_exchange():
    for capa in ("ui", "services"):
        for archivo in sorted((_APP / capa).rglob("*.py")):
            texto = archivo.read_text(encoding="utf-8").lower()
            assert "binance_api" not in texto, archivo


def test_el_token_de_telegram_solo_lo_usan_las_notificaciones_y_los_scripts():
    """P4: ni la interfaz, ni los servicios, ni la ejecución, ni la IA leen el
    token del bot de Telegram."""
    for capa in ("ui", "services", "execution", "ai", "domain", "results", "data", "repositories"):
        carpeta = _APP / capa
        if not carpeta.is_dir():
            continue
        for archivo in sorted(carpeta.rglob("*.py")):
            assert "telegram_bot_token" not in archivo.read_text(encoding="utf-8").lower(), archivo


def test_nadie_en_la_aplicacion_importa_los_experimentos():
    """Enmienda E6: Kronos es un componente aislado y NO entra a compras, score ni
    ejecución. Si alguna capa de `app/` lo importara, dejaría de ser opcional."""
    importadores = [x for x in _importadores("app.experiments") if not x.startswith("experiments/")]
    assert importadores == [], importadores


def test_el_dominio_y_los_experimentos_no_importan_librerias_de_aprendizaje_automatico_en_el_dominio():
    """El dominio (reglas del experimento incluidas) es Python puro: torch, pandas y
    numpy solo viven en el adaptador de `app/experiments/kronos/predictor.py`."""
    pesadas = ("torch", "numpy", "pandas", "transformers", "huggingface_hub", "safetensors")
    for archivo in sorted((_APP / "domain").rglob("*.py")):
        malos = [m for m in _imports(archivo) if m.split(".")[0] in pesadas]
        assert malos == [], (archivo, malos)
    for archivo in sorted((_APP / "experiments").rglob("*.py")):
        if archivo.name == "predictor.py" or archivo.name == "pesos.py":
            continue
        malos = [m for m in _imports(archivo) if m.split(".")[0] in pesadas]
        assert malos == [], (archivo, malos)


def test_los_experimentos_no_tocan_credenciales():
    """P4: el código de terceros corre sin llaves del exchange, de Anthropic ni de Telegram."""
    for archivo in sorted((_APP / "experiments").rglob("*.py")):
        texto = archivo.read_text(encoding="utf-8").lower()
        for secreto in ("binance_api", "anthropic_api", "telegram_bot_token", "database_url"):
            assert secreto not in texto, (archivo, secreto)
