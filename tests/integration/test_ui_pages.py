"""Páginas de la interfaz (Paso 10a) con `streamlit.testing.v1.AppTest`.

Cada página se ejecuta contra Postgres real: los servicios leen la sesión de
prueba (con los datos del escenario del Paso 9) y se comprueba que la página
se dibuja sin errores y muestra lo guardado, con `n/d` donde falta un dato.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.domain.enums import IntencionOrden
from app.services import commands, views
from tests.integration.test_commands import _pausar_sin_posiciones, _pendiente
from tests.integration.test_results_pipeline import FakePrices, _abrir, _t, _universo, seis_operaciones  # noqa: F401

PAGINAS = ["dashboard", "analisis", "posiciones", "operaciones", "performance", "validacion", "decisiones", "perfil", "sistema"]
TIEMPO = 60
_APP_UI = Path(__file__).resolve().parents[2] / "app" / "ui" / "app.py"


def _pagina(nombre: str) -> None:
    import importlib

    importlib.import_module(f"app.ui.paginas.{nombre}").render()


@pytest.fixture
def ui(db_session, monkeypatch):
    """Las vistas leen la sesión de prueba (sus datos viven en una transacción)."""

    @contextmanager
    def _sesion(factory=None):
        yield db_session

    monkeypatch.setattr(views, "read_session", _sesion)
    monkeypatch.setattr(views, "_ahora", lambda: _t(0, 30))

    def abrir(nombre: str) -> AppTest:
        return AppTest.from_function(_pagina, args=(nombre,), default_timeout=TIEMPO).run()

    return abrir


def _textos(at: AppTest) -> str:
    partes = [e.value for e in at.markdown] + [e.value for e in at.caption] + [e.value for e in at.info]
    partes += [e.value for e in at.warning] + [e.value for e in at.error] + [e.value for e in at.success]
    partes += [f"{m.label} {m.value}" for m in at.metric] + [e.value for e in at.title] + [e.value for e in at.subheader]
    return "\n".join(str(p) for p in partes)


@pytest.mark.parametrize("pagina", PAGINAS)
def test_cada_pagina_se_dibuja_sin_errores_con_la_base_vacia(ui, pagina):
    at = ui(pagina)
    assert not at.exception, [e.value for e in at.exception]
    assert at.title  # todas tienen título


@pytest.mark.parametrize("pagina", PAGINAS)
def test_cada_pagina_se_dibuja_sin_errores_con_datos(ui, seis_operaciones, pagina):  # noqa: F811
    at = ui(pagina)
    assert not at.exception, [e.value for e in at.exception]


def test_dashboard_vacio_dice_que_no_hay_operaciones_y_usa_nd(ui):
    at = ui("dashboard")
    texto = _textos(at)
    assert "Todavía no hay operaciones cerradas" in texto
    assert "n/d" in texto  # sin equity ni drawdown: no se muestra un 0 inventado


def test_dashboard_con_datos_muestra_resultados_guardados(ui, seis_operaciones):  # noqa: F811
    at = ui("dashboard")
    texto = _textos(at)
    assert "Operaciones 6" in texto and "Win rate 50.00 %" in texto
    assert "Paper trading: no hay dinero real" in texto


def test_las_metricas_con_definicion_llevan_su_tooltip(ui, seis_operaciones):  # noqa: F811
    at = ui("dashboard")
    con_ayuda = {m.label: m.help for m in at.metric if m.help}
    clave = next(k for k in con_ayuda if k.startswith("Expectancy"))
    assert "Limitaciones" in con_ayuda[clave] and "definición v" in con_ayuda[clave]


def test_operaciones_lista_y_muestra_el_detalle_de_la_primera(ui, seis_operaciones):  # noqa: F811
    at = ui("operaciones")
    assert len(at.dataframe) >= 2  # tabla + factores/otras
    texto = _textos(at)
    assert "Tesis al entrar" in texto and "Lo que el sistema sabía al decidir" in texto


def test_operaciones_filtra_por_motivo_de_salida_sin_coincidencias(ui, seis_operaciones):  # noqa: F811
    at = ui("operaciones")
    motivos = [s for s in at.selectbox if s.label == "Motivo de salida"][0]
    otro = next(e.value for e in IntencionOrden if e.value not in {"STOP_LOSS", "TRAILING_STOP", "TAKE_PROFIT_PARTIAL"}
                and e.value != "ENTRY")
    motivos.set_value(otro).run()
    assert not at.exception
    assert "No hay operaciones cerradas que cumplan el filtro" in _textos(at)


def test_posiciones_vacia_y_con_una_abierta(ui, db_session):
    assert "No hay posiciones abiertas" in _textos(ui("posiciones"))
    _abrir(db_session)
    at = ui("posiciones")
    assert not at.exception and "No hay posiciones abiertas" not in _textos(at)
    assert any(e.label.startswith(("BTCUSDT", "ETHUSDT", "SOLUSDT")) or "USDT" in e.label for e in at.expander)


def test_validacion_vacia_declara_muestra_insuficiente_y_pendientes(ui):
    at = ui("validacion")
    assert "MUESTRA_INSUFICIENTE" in _textos(at)
    etiquetas = " ".join(e.label for e in at.expander)
    assert "PENDIENTE" in etiquetas and "Disponible" in etiquetas


def test_validacion_con_seis_operaciones_es_preliminar_y_marca_discriminacion(ui, seis_operaciones):  # noqa: F811
    texto = _textos(ui("validacion"))
    assert "PRELIMINAR" in texto and "solo orientativo" in texto
    assert "Discriminación 20.38 USD" in texto


def test_performance_cambia_de_periodo_sin_errores(ui, seis_operaciones):  # noqa: F811
    at = ui("performance")
    at.selectbox[0].set_value("Últimos 7 días").run()
    assert not at.exception


def test_decisiones_y_analisis_sin_ciclos_lo_dicen(ui):
    assert "ningún ciclo" in _textos(ui("decisiones"))
    assert "ningún ciclo" in _textos(ui("analisis"))


def test_analisis_declara_pendiente_el_analisis_por_factor(ui):
    assert "PENDIENTE" in _textos(ui("analisis"))


def test_sistema_muestra_base_ok_y_sin_saldo_declarado(ui):
    at = ui("sistema")
    assert not at.exception
    assert "Conexión OK" in _textos(at)


def test_la_aplicacion_completa_arranca_en_el_dashboard(ui):
    at = AppTest.from_file(str(_APP_UI), default_timeout=TIEMPO).run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Paper trading" in c.value for c in at.sidebar.caption)
    assert at.title[0].value == "Dashboard"


# --- acciones con confirmación (Paso 10c) ---


@pytest.fixture
def con_acciones(ui, db_session, monkeypatch):
    @contextmanager
    def _sesion():
        yield db_session

    monkeypatch.setattr(commands, "_abrir_sesion", _sesion)
    monkeypatch.setattr(commands, "_ahora", lambda: _t(5))
    monkeypatch.setattr(commands, "_fuente_de_precios", lambda: FakePrices(_universo(ETHUSDT="105")))
    return ui


def _boton(at: AppTest, etiqueta: str):
    return next(b for b in at.button if b.label == etiqueta)


def test_un_boton_nunca_ejecuta_sin_confirmar(con_acciones, db_session):
    from app.domain.enums import EstadoOrden
    from app.repositories.orders import get_orden

    oid = _pendiente(db_session)
    at = con_acciones("posiciones")
    _boton(at, "Aprobar entrada").click().run()
    assert any("ABRIR una posición paper" in w.value for w in at.warning)
    assert get_orden(db_session, oid).estado == EstadoOrden.PENDING_APPROVAL  # aún no pasó nada


def test_cancelar_no_cambia_nada(con_acciones, db_session):
    from app.domain.enums import EstadoOrden
    from app.repositories.orders import get_orden

    oid = _pendiente(db_session)
    at = con_acciones("posiciones")
    _boton(at, "Aprobar entrada").click().run()
    _boton(at, "Cancelar").click().run()
    assert get_orden(db_session, oid).estado == EstadoOrden.PENDING_APPROVAL
    assert any(b.label == "Aprobar entrada" for b in at.button)


def test_aprobar_confirmado_abre_la_posicion_y_muestra_el_resultado(con_acciones, db_session):
    from app.domain.enums import EstadoOrden
    from app.repositories.orders import get_orden
    from app.repositories.positions import get_open_posicion_by_symbol

    oid = _pendiente(db_session)
    at = con_acciones("posiciones")
    _boton(at, "Aprobar entrada").click().run()
    _boton(at, "Confirmar").click().run()
    assert not at.exception
    assert get_orden(db_session, oid).estado == EstadoOrden.FILLED
    assert get_open_posicion_by_symbol(db_session, "ETHUSDT").entry_price == 105
    assert any("Entrada aprobada y ejecutada en paper" in s.value for s in at.success)
    assert not any(b.label == "Aprobar entrada" for b in at.button)  # ya no está pendiente
    at.run()  # el resultado se muestra una sola vez
    assert not any("Entrada aprobada" in s.value for s in at.success)


def test_rechazar_confirmado_con_motivo(con_acciones, db_session):
    from app.domain.enums import EstadoOrden
    from app.repositories.orders import get_orden

    oid = _pendiente(db_session)
    at = con_acciones("posiciones")
    _boton(at, "Rechazar entrada").click().run()
    at.text_input[0].set_value("no me convence").run()
    _boton(at, "Confirmar").click().run()
    orden = get_orden(db_session, oid)
    assert orden.estado == EstadoOrden.REJECTED and orden.detail == "no me convence"
    assert any("Entrada rechazada" in s.value for s in at.success)


def test_en_pausa_aprobar_aparece_deshabilitado_y_rechazar_sigue_disponible(con_acciones, db_session):
    _pendiente(db_session)
    _pausar_sin_posiciones(db_session)
    at = con_acciones("posiciones")
    assert _boton(at, "Aprobar entrada").disabled is True
    assert _boton(at, "Rechazar entrada").disabled is False
    assert "no se pueden aprobar entradas nuevas" in _textos(at)


def test_reanudar_solo_aparece_en_pausa_y_pide_confirmacion(con_acciones, db_session):
    assert not any(b.label == "Reanudar el sistema" for b in con_acciones("sistema").button)
    _pausar_sin_posiciones(db_session)
    at = con_acciones("sistema")
    _boton(at, "Reanudar el sistema").click().run()
    assert any("REANUDAR el sistema" in w.value for w in at.warning)
    _boton(at, "Confirmar").click().run()
    assert not at.exception
    assert any("Sistema reanudado" in s.value for s in at.success)
    assert not any(b.label == "Reanudar el sistema" for b in at.button)


def test_un_fallo_de_la_accion_se_muestra_como_aviso_sin_traza(con_acciones, db_session, monkeypatch):
    oid = _pendiente(db_session)
    at = con_acciones("posiciones")
    monkeypatch.setattr("app.services.commands.approve_pending_entry", lambda *a, **k: 1 / 0)
    _boton(at, "Aprobar entrada").click().run()
    _boton(at, "Confirmar").click().run()
    assert not at.exception
    assert any("no se cambió nada" in w.value for w in at.warning)


# --- perfil / entrevista (Paso 10b) ---


@pytest.fixture
def entrevista(ui, db_session, monkeypatch):
    """Entrevista con el modelo simulado y escritura en la sesión de prueba."""
    import json

    from app.ai.agents import interviewer
    from app.config import get_settings
    from app.services import interview
    from tests.integration.test_interview import ModeloSimulado
    from tests.unit.test_interviewer_unit import PERFIL

    @contextmanager
    def _sesion():
        yield db_session

    monkeypatch.setattr(interview, "_abrir_sesion", _sesion)
    monkeypatch.setattr(interview, "settings", lambda: get_settings().model_copy(update={"anthropic_api_key": "k"}))

    def preparar(*respuestas):
        modelo = ModeloSimulado(*respuestas)
        monkeypatch.setattr(interviewer, "call_chat", modelo)
        return modelo

    return ui, preparar, json.dumps(PERFIL)


def test_perfil_sin_datos_invita_a_iniciar_la_entrevista(ui):
    at = ui("perfil")
    assert "Todavía no hay un perfil guardado" in _textos(at)
    assert any(b.label == "Iniciar entrevista" for b in at.button)
    assert "no cambia el análisis" in _textos(at)  # deja claro que es informativo


def test_entrevista_completa_hasta_guardar_el_perfil(entrevista, db_session):
    ui_, preparar, perfil_json = entrevista
    modelo = preparar("¿Cuál es tu horizonte?", "¿Qué pérdida toleras?", perfil_json)
    at = ui_("perfil")
    _boton(at, "Iniciar entrevista").click().run()
    assert not at.exception
    assert any("¿Cuál es tu horizonte?" in m.value for m in at.chat_message[0].markdown)
    assert "Pregunta 1 de 8" in _textos(at)

    at.chat_input[0].set_value("24 meses").run()
    assert "Pregunta 2 de 8" in _textos(at)
    at.chat_input[0].set_value("20 %").run()
    assert "Perfil propuesto" in _textos(at) and not at.chat_input  # ya no pide más respuestas
    assert len(modelo.llamadas) == 3

    _boton(at, "Guardar este perfil").click().run()
    assert any("GUARDAR este perfil" in w.value for w in at.warning)
    _boton(at, "Confirmar").click().run()
    assert not at.exception
    assert any("Perfil guardado" in s.value for s in at.success)
    assert "Perfil vigente" in _textos(at) and any(b.label == "Rehacer la entrevista" for b in at.button)
    from app.services.profile import get_perfil_view

    assert get_perfil_view(db_session).vigente.horizonte_meses == 24


def test_entrevista_sin_confirmar_no_guarda_nada(entrevista, db_session):
    ui_, preparar, perfil_json = entrevista
    preparar("¿Horizonte?", perfil_json)
    at = ui_("perfil")
    _boton(at, "Iniciar entrevista").click().run()
    at.chat_input[0].set_value("12 meses").run()
    _boton(at, "Guardar este perfil").click().run()
    _boton(at, "Cancelar").click().run()
    from app.services.profile import get_perfil_view

    assert get_perfil_view(db_session).vigente is None


def test_un_fallo_del_modelo_se_muestra_y_se_puede_reintentar(entrevista):
    ui_, preparar, _ = entrevista
    preparar("   ", "¿Horizonte?")  # primero vacío (error), luego una pregunta
    at = ui_("perfil")
    _boton(at, "Iniciar entrevista").click().run()
    assert any("vacío" in e.value for e in at.error)
    _boton(at, "Reintentar").click().run()
    assert not at.error and any("¿Horizonte?" in m.value for m in at.chat_message[0].markdown)


def test_cancelar_la_entrevista_vuelve_al_inicio(entrevista):
    ui_, preparar, _ = entrevista
    preparar("¿Horizonte?")
    at = ui_("perfil")
    _boton(at, "Iniciar entrevista").click().run()
    _boton(at, "Cancelar la entrevista").click().run()
    assert any(b.label == "Iniciar entrevista" for b in at.button) and not at.chat_message
