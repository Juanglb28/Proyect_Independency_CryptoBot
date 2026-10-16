"""Test de `scripts.run_daily_cycle.explicar_sin_propuestas`: solo el texto que
explica por qué un ciclo terminó sin propuestas — sin base de datos, red ni
Anthropic (observación 2026-10-04: la salida solo decía "Sin propuestas este
ciclo." sin distinguir las causas posibles)."""

from __future__ import annotations

import uuid
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
