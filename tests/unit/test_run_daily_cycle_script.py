"""Test de `scripts.run_daily_cycle.explicar_sin_propuestas`: solo el texto que
explica por qué un ciclo terminó sin propuestas — sin base de datos, red ni
Anthropic (observación 2026-10-04: la salida solo decía "Sin propuestas este
ciclo." sin distinguir las causas posibles)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.orchestration.daily_cycle import DailyCycleResult
from scripts.run_daily_cycle import explicar_sin_propuestas


def _result(**overrides) -> DailyCycleResult:
    defaults = dict(
        ciclo_id=uuid.uuid4(),
        candidatos_evaluados=("ETHUSDT",),
        candidatos_excluidos_por_calidad=(),
        propuesta_ids=(),
        cost_usd=Decimal("0.1"),
    )
    defaults.update(overrides)
    return DailyCycleResult(**defaults)


def test_sin_candidatos_del_prefiltro():
    texto = "\n".join(explicar_sin_propuestas(_result(candidatos_evaluados=())))
    assert "pre-filtro" in texto


def test_todos_excluidos_por_calidad_no_llamo_al_consolidador():
    texto = "\n".join(
        explicar_sin_propuestas(
            _result(candidatos_evaluados=("ETHUSDT",), candidatos_excluidos_por_calidad=("ETHUSDT",))
        )
    )
    assert "calidad de datos" in texto
    assert "no se llamo" in texto


def test_consolidador_no_invertir_muestra_su_razon_y_confianza():
    texto = "\n".join(
        explicar_sin_propuestas(
            _result(
                consolidador_recomendacion="no_invertir",
                consolidador_razon_no_invertir="ningun candidato sostiene una tesis",
                consolidador_confianza_global="alta",
            )
        )
    )
    assert "NO invertir" in texto
    assert "ningun candidato sostiene una tesis" in texto
    assert "alta" in texto


def test_caso_inesperado_no_inventa_una_causa():
    texto = "\n".join(explicar_sin_propuestas(_result(consolidador_recomendacion="invertir")))
    assert "no determinado" in texto


# ── Aviso por Telegram de la corrida manual (2026-10-11) ───────────────────


def _settings_con_telegram(token="t", chat="c"):
    from unittest.mock import MagicMock

    s = MagicMock()
    s.telegram_bot_token = token
    s.telegram_chat_id = chat
    s.ai_daily_budget_usd = Decimal("2.00")
    return s


def test_sin_telegram_configurado_avisa_que_el_resumen_queda_guardado(capsys):
    from unittest.mock import patch

    from scripts.run_daily_cycle import _enviar_avisos_pendientes

    with patch("scripts.run_daily_cycle.dispatch_pending_alerts") as despacho:
        _enviar_avisos_pendientes(object(), _settings_con_telegram(token=None))

    despacho.assert_not_called()
    assert "Telegram no está configurado" in capsys.readouterr().out


def test_un_fallo_al_enviar_no_tumba_la_corrida_y_se_dice(capsys):
    from unittest.mock import patch

    from scripts.run_daily_cycle import _enviar_avisos_pendientes

    with patch("scripts.run_daily_cycle.dispatch_pending_alerts", side_effect=RuntimeError("boom")):
        _enviar_avisos_pendientes(object(), _settings_con_telegram())  # no debe lanzar

    salida = capsys.readouterr().out
    assert "RuntimeError" in salida and "queda pendiente" in salida


def test_cuenta_los_avisos_enviados(capsys):
    from unittest.mock import patch

    from app.notifications.dispatcher import ResultadoDespacho
    from scripts.run_daily_cycle import _enviar_avisos_pendientes

    with patch("scripts.run_daily_cycle.dispatch_pending_alerts", return_value=ResultadoDespacho(enviadas=1)):
        _enviar_avisos_pendientes(object(), _settings_con_telegram())

    assert "1 aviso(s) enviado(s)" in capsys.readouterr().out


def test_main_guarda_y_envia_el_resumen_aunque_el_detalle_falle():
    """El resumen sale ANTES de imprimir el detalle de la corrida."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock, patch

    import scripts.run_daily_cycle as mod

    orden: list[str] = []

    @contextmanager
    def scope(_fabrica):
        yield MagicMock()

    with (
        patch.object(mod, "configure_logging"),
        patch.object(mod, "get_settings", return_value=_settings_con_telegram()),
        patch.object(mod, "get_engine"),
        patch.object(mod, "get_sessionmaker"),
        patch.object(mod, "session_scope", scope),
        patch.object(mod, "_confirmar_ciclo_reciente", return_value=True),
        patch.object(mod, "run_daily_cycle", return_value=_result()),
        patch.object(mod, "registrar_resumen_ciclo", side_effect=lambda *a, **k: orden.append("resumen")),
        patch.object(mod, "_enviar_avisos_pendientes", side_effect=lambda *a, **k: orden.append("envio")),
        patch.object(mod, "_imprimir_control_de_gastos", side_effect=lambda *a, **k: orden.append("detalle")),
    ):
        assert mod.main() == 0

    assert orden[:2] == ["resumen", "envio"] and "detalle" in orden


def test_main_si_el_ciclo_falla_avisa_el_fallo_y_relanza():
    import pytest
    from contextlib import contextmanager
    from unittest.mock import MagicMock, patch

    import scripts.run_daily_cycle as mod

    @contextmanager
    def scope(_fabrica):
        yield MagicMock()

    error = RuntimeError("Anthropic caído")
    with (
        patch.object(mod, "configure_logging"),
        patch.object(mod, "get_settings", return_value=_settings_con_telegram()),
        patch.object(mod, "get_engine"),
        patch.object(mod, "get_sessionmaker"),
        patch.object(mod, "session_scope", scope),
        patch.object(mod, "_confirmar_ciclo_reciente", return_value=True),
        patch.object(mod, "run_daily_cycle", side_effect=error),
        patch.object(mod, "registrar_ciclo_fallido") as fallido,
        patch.object(mod, "registrar_resumen_ciclo") as resumen,
        patch.object(mod, "_enviar_avisos_pendientes") as envio,
    ):
        with pytest.raises(RuntimeError, match="Anthropic caído"):
            mod.main()

    fallido.assert_called_once()
    assert fallido.call_args.kwargs["error"] is error and fallido.call_args.kwargs["origen"].value == "MANUAL"
    envio.assert_called_once()
    resumen.assert_not_called()


# ── Advertencia de saldo bajo antes de una corrida manual (2026-10-11) ───────


def _settings_con_saldo(saldo="0.34"):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    s = MagicMock()
    s.anthropic_saldo_declarado_usd = Decimal(saldo) if saldo is not None else None
    s.anthropic_saldo_declarado_en = datetime(2026, 10, 11, tzinfo=timezone.utc)
    s.ai_min_balance_per_cycle_usd = Decimal("0.60")
    return s


def _con_saldo_estimado(valor):
    from contextlib import contextmanager
    from unittest.mock import MagicMock, patch

    import scripts.run_daily_cycle as mod

    @contextmanager
    def scope(_fabrica):
        yield MagicMock()

    return patch.object(mod, "session_scope", scope), patch.object(mod, "leer_saldo_estimado_ia", return_value=valor)


def test_saldo_bajo_advierte_antes_de_gastar_pero_no_bloquea(capsys):
    from scripts.run_daily_cycle import _advertir_saldo_bajo

    p1, p2 = _con_saldo_estimado(Decimal("0.34"))
    with p1, p2:
        _advertir_saldo_bajo(object(), _settings_con_saldo())  # no lanza ni pide confirmación

    salida = capsys.readouterr().out
    assert "ADVERTENCIA" in salida and "$0.34 USD" in salida and "$0.60" in salida


def test_saldo_suficiente_no_imprime_nada(capsys):
    from scripts.run_daily_cycle import _advertir_saldo_bajo

    p1, p2 = _con_saldo_estimado(Decimal("3.00"))
    with p1, p2:
        _advertir_saldo_bajo(object(), _settings_con_saldo("3.00"))

    assert capsys.readouterr().out == ""


def test_sin_saldo_declarado_no_imprime_nada(capsys):
    from scripts.run_daily_cycle import _advertir_saldo_bajo

    p1, p2 = _con_saldo_estimado(None)
    with p1, p2:
        _advertir_saldo_bajo(object(), _settings_con_saldo(None))

    assert capsys.readouterr().out == ""


def test_si_no_se_puede_leer_el_saldo_la_advertencia_no_rompe_la_corrida(capsys):
    from unittest.mock import patch

    import scripts.run_daily_cycle as mod

    with patch.object(mod, "session_scope", side_effect=RuntimeError("base caída")):
        mod._advertir_saldo_bajo(object(), _settings_con_saldo())  # no debe lanzar

    assert capsys.readouterr().out == ""


# ── Confirmación si ya hay un ciclo reciente (2026-10-21) ────────────────────

_AHORA = datetime(2026, 10, 20, 3, 30, tzinfo=timezone.utc)


def _fila_ciclo(*, inicio, fin, error=None):
    from unittest.mock import MagicMock

    fila = MagicMock()
    fila.id = uuid.uuid4()
    fila.started_at = inicio
    fila.finished_at = fin
    fila.error_message = error
    return fila


def _confirmar(filas, *, forzar=False, es_interactivo=True, respuesta="s", db_falla=False):
    """Corre `_confirmar_ciclo_reciente` con la base simulada. Devuelve (resultado, preguntas)."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock, patch

    import scripts.run_daily_cycle as mod

    preguntas: list[str] = []

    @contextmanager
    def scope(_fabrica):
        if db_falla:
            raise RuntimeError("base caída")
        yield MagicMock()

    def pedir(texto):
        preguntas.append(texto)
        if isinstance(respuesta, BaseException):
            raise respuesta
        return respuesta

    with (
        patch.object(mod, "session_scope", scope),
        patch.object(mod, "list_ciclos_iniciados_desde", return_value=filas),
    ):
        resultado = mod._confirmar_ciclo_reciente(
            object(), forzar=forzar, ahora=_AHORA, es_interactivo=es_interactivo, pedir=pedir
        )
    return resultado, preguntas


def _ciclo_de_hace_18_min():
    return _fila_ciclo(inicio=_AHORA - timedelta(minutes=24), fin=_AHORA - timedelta(minutes=18))


def test_sin_ciclos_recientes_no_pregunta_nada():
    resultado, preguntas = _confirmar([])
    assert resultado is True and preguntas == []


def test_ciclo_reciente_pregunta_y_continua_si_responde_que_si(capsys):
    resultado, preguntas = _confirmar([_ciclo_de_hace_18_min()], respuesta="s")
    assert resultado is True and len(preguntas) == 1
    assert "ciclo completado hace 18 min" in capsys.readouterr().out


def test_ciclo_reciente_cancela_con_cualquier_respuesta_que_no_sea_si():
    for respuesta in ("n", "", "no", "tal vez"):
        resultado, preguntas = _confirmar([_ciclo_de_hace_18_min()], respuesta=respuesta)
        assert resultado is False and len(preguntas) == 1


def test_acepta_si_con_tilde_y_mayusculas():
    for respuesta in ("sí", "SI", " Sí ", "y", "YES"):
        assert _confirmar([_ciclo_de_hace_18_min()], respuesta=respuesta)[0] is True


def test_forzar_no_pregunta_ni_consulta_la_base():
    resultado, preguntas = _confirmar([_ciclo_de_hace_18_min()], forzar=True, db_falla=True)
    assert resultado is True and preguntas == []


def test_sin_terminal_interactiva_cancela_sin_preguntar(capsys):
    resultado, preguntas = _confirmar([_ciclo_de_hace_18_min()], es_interactivo=False)
    assert resultado is False and preguntas == []
    assert "--forzar" in capsys.readouterr().out


def test_sin_terminal_interactiva_pero_sin_ciclo_reciente_sigue():
    resultado, preguntas = _confirmar([], es_interactivo=False)
    assert resultado is True and preguntas == []


def test_entrada_cerrada_eof_cancela():
    resultado, _ = _confirmar([_ciclo_de_hace_18_min()], respuesta=EOFError())
    assert resultado is False


def test_si_no_se_puede_consultar_la_base_avisa_y_sigue(capsys):
    resultado, preguntas = _confirmar([], db_falla=True)
    assert resultado is True and preguntas == []
    assert "no se pudo comprobar" in capsys.readouterr().out


def test_ciclo_fallido_reciente_no_pide_confirmacion():
    fallido = _fila_ciclo(inicio=_AHORA - timedelta(minutes=30), fin=_AHORA - timedelta(minutes=25), error="boom")
    resultado, preguntas = _confirmar([fallido])
    assert resultado is True and preguntas == []


def test_main_cancela_sin_gastar_si_no_se_confirma(capsys):
    from contextlib import contextmanager
    from unittest.mock import MagicMock, patch

    import scripts.run_daily_cycle as mod

    @contextmanager
    def scope(_fabrica):
        yield MagicMock()

    with (
        patch.object(mod, "configure_logging"),
        patch.object(mod, "get_settings", return_value=_settings_con_telegram()),
        patch.object(mod, "get_engine"),
        patch.object(mod, "get_sessionmaker"),
        patch.object(mod, "session_scope", scope),
        patch.object(mod, "_confirmar_ciclo_reciente", return_value=False),
        patch.object(mod, "run_daily_cycle") as ciclo,
        patch.object(mod, "registrar_ciclo_fallido") as fallido,
    ):
        assert mod.main([]) == 2

    ciclo.assert_not_called()
    fallido.assert_not_called()
    assert "no se lanzó ningún ciclo" in capsys.readouterr().out


def test_main_pasa_forzar_a_la_confirmacion():
    from contextlib import contextmanager
    from unittest.mock import MagicMock, patch

    import scripts.run_daily_cycle as mod

    @contextmanager
    def scope(_fabrica):
        yield MagicMock()

    with (
        patch.object(mod, "configure_logging"),
        patch.object(mod, "get_settings", return_value=_settings_con_telegram()),
        patch.object(mod, "get_engine"),
        patch.object(mod, "get_sessionmaker"),
        patch.object(mod, "session_scope", scope),
        patch.object(mod, "_confirmar_ciclo_reciente", return_value=True) as confirmar,
        patch.object(mod, "run_daily_cycle", return_value=_result()),
        patch.object(mod, "registrar_resumen_ciclo"),
        patch.object(mod, "_enviar_avisos_pendientes"),
        patch.object(mod, "_imprimir_control_de_gastos"),
    ):
        assert mod.main(["--forzar"]) == 0

    assert confirmar.call_args.kwargs["forzar"] is True
