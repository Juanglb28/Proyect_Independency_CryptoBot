"""Tests unitarios del scheduler (Paso 11b).

Cada test usa mocks completos — sin base de datos, sin Binance, sin APScheduler.
Se prueban las funciones individuales de los jobs, la lógica del lock del ciclo
diario, el heartbeat y el check de DB.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.domain.enums import TipoAlerta


# ── Helpers / fakes ────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime(2026, 10, 9, 14, 5, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _FakeResultadoTick:
    equity: Decimal | None = Decimal("950")
    drawdown_pct: Decimal | None = Decimal("-5")
    posiciones_abiertas: int = 2
    sin_precio: tuple[str, ...] = ()
    errores: tuple = ()

    @property
    def hubo_problemas(self) -> bool:
        return bool(self.sin_precio or self.errores)


@dataclass(frozen=True)
class _FakeHourlyCycleResult:
    hora_utc: int = 8
    timeframes_ingested: tuple[str, ...] = ("1h",)
    backfill_results: tuple = ()
    indicator_results: tuple = ()
    prefilter: None = None
    new_candidates: tuple = ()
    ultimo_ciclo_id: uuid.UUID | None = None
    elapsed_seconds: float = 0.5


@dataclass(frozen=True)
class _FakeNewCandidate:
    symbol: str = "ETHUSDT"
    score_actual: Decimal = Decimal("52")
    score_en_ultimo_ciclo: Decimal | None = None
    estado_en_ultimo_ciclo: str | None = None


@dataclass(frozen=True)
class _FakeDailyCycleResult:
    ciclo_id: uuid.UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")
    candidatos_evaluados: tuple[str, ...] = ("ETHUSDT",)
    candidatos_excluidos_por_calidad: tuple[str, ...] = ()
    propuesta_ids: tuple[uuid.UUID, ...] = ()
    cost_usd: Decimal = Decimal("0.05")
    consolidador_recomendacion: str | None = None
    consolidador_razon_no_invertir: str | None = None


@dataclass(frozen=True)
class _FakeBackfillResult:
    symbol: str = "BTCUSDT"
    timeframe: str = "1h"
    fetched: int = 10
    inserted: int = 5
    pages: int = 1
    last_open_time: datetime | None = None
    hit_page_safety_limit: bool = False
    updated: int = 0


@dataclass(frozen=True)
class _FakeIndicatorResult:
    symbol: str = "BTCUSDT"
    timeframe: str = "1h"
    candles_used: int = 100
    calculated: int = 10
    inserted: int = 10


@dataclass(frozen=True)
class _FakeResultadoDespacho:
    enviadas: int = 0
    fallidas: int = 0


class _FakeDiscrepancia:
    def __init__(self, codigo_value: str, detalle: str):
        self.codigo = MagicMock(value=codigo_value)
        self.detalle = detalle


def _make_state(sender=None) -> MagicMock:
    """Crea un _SharedState falso con las propiedades necesarias."""
    state = MagicMock()
    state.session_factory = MagicMock()
    state.settings = MagicMock()
    state.settings.paper_capital_usd = Decimal("1000")
    state.price_source = MagicMock()
    state.sender = sender
    return state


# ═══════════════════════════════════════════════════════════════════════
# Tests de _job_monitor
# ═══════════════════════════════════════════════════════════════════════


class TestJobMonitor:
    """Tests de _job_monitor: el tick del monitor dentro del scheduler."""

    @patch("app.orchestration.scheduler._despachar_alertas")
    @patch("app.orchestration.scheduler.run_monitor_tick")
    @patch("app.orchestration.scheduler.session_scope")
    def test_tick_normal_despacha_alertas(self, mock_scope, mock_tick, mock_despacho):
        from app.orchestration.scheduler import _job_monitor

        mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        mock_tick.return_value = _FakeResultadoTick()

        state = _make_state()
        _job_monitor(state)

        mock_tick.assert_called_once()
        mock_despacho.assert_called_once()

    @patch("app.orchestration.scheduler._despachar_alertas")
    @patch("app.orchestration.scheduler.avisar_discrepancia")
    @patch("app.orchestration.scheduler.session_scope")
    def test_reconciliation_blocked_registra_aviso_y_despacha(
        self, mock_scope, mock_avisar, mock_despacho
    ):
        from app.execution.monitor import ReconciliationBlocked
        from app.orchestration.scheduler import _job_monitor

        discrepancias = [_FakeDiscrepancia("CRIT_01", "cantidad no cuadra")]

        # Primer session_scope lanza ReconciliationBlocked, segundo funciona normal
        ctx_error = MagicMock()
        ctx_error.__enter__ = MagicMock(side_effect=ReconciliationBlocked(discrepancias))
        ctx_error.__exit__ = MagicMock(return_value=False)

        ctx_ok = MagicMock()
        ctx_ok.__enter__ = MagicMock(return_value=MagicMock())
        ctx_ok.__exit__ = MagicMock(return_value=False)

        mock_scope.side_effect = [ctx_error, ctx_ok]

        state = _make_state()
        _job_monitor(state)

        mock_avisar.assert_called_once()
        mock_despacho.assert_called_once()

    @patch("app.orchestration.scheduler._despachar_alertas")
    @patch("app.orchestration.scheduler.session_scope")
    def test_error_generico_no_mata_al_scheduler(self, mock_scope, mock_despacho):
        from app.orchestration.scheduler import _job_monitor

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(side_effect=ConnectionError("DB offline"))
        ctx.__exit__ = MagicMock(return_value=False)
        mock_scope.return_value = ctx

        state = _make_state()
        # No debe lanzar
        _job_monitor(state)
        # No se despacha porque el chequeo falló antes
        mock_despacho.assert_not_called()

    @patch("app.orchestration.scheduler._despachar_alertas")
    @patch("app.orchestration.scheduler.run_monitor_tick")
    @patch("app.orchestration.scheduler.session_scope")
    def test_tick_con_problemas_logea_warning(self, mock_scope, mock_tick, mock_despacho):
        from app.orchestration.scheduler import _job_monitor

        mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        mock_tick.return_value = _FakeResultadoTick(sin_precio=("BTCUSDT",))

        state = _make_state()
        _job_monitor(state)

        mock_despacho.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# Tests de _job_hourly
# ═══════════════════════════════════════════════════════════════════════


class TestJobHourly:
    """Tests del ciclo horario dentro del scheduler."""

    @patch("app.orchestration.scheduler._ejecutar_ciclo_diario")
    @patch("app.orchestration.scheduler.run_hourly_cycle")
    @patch("app.orchestration.scheduler.session_scope")
    def test_hora_0_se_salta(self, mock_scope, mock_hourly, mock_daily):
        from app.orchestration.scheduler import _job_hourly

        state = _make_state()
        with patch("app.orchestration.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 9, 0, 5, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            _job_hourly(state)

        mock_hourly.assert_not_called()
        mock_daily.assert_not_called()

    @patch("app.orchestration.scheduler._ejecutar_ciclo_diario")
    @patch("app.orchestration.scheduler.run_hourly_cycle")
    @patch("app.orchestration.scheduler.session_scope")
    def test_sin_candidatos_no_dispara_diario(self, mock_scope, mock_hourly, mock_daily):
        from app.orchestration.scheduler import _job_hourly

        mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        mock_hourly.return_value = _FakeHourlyCycleResult(hora_utc=8)

        state = _make_state()
        with patch("app.orchestration.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 9, 8, 5, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            _job_hourly(state)

        mock_hourly.assert_called_once()
        mock_daily.assert_not_called()

    @patch("app.orchestration.scheduler._ejecutar_ciclo_diario")
    @patch("app.orchestration.scheduler.run_hourly_cycle")
    @patch("app.orchestration.scheduler.session_scope")
    def test_con_candidatos_dispara_diario(self, mock_scope, mock_hourly, mock_daily):
        from app.orchestration.scheduler import _job_hourly

        mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        mock_hourly.return_value = _FakeHourlyCycleResult(
            hora_utc=8,
            new_candidates=(_FakeNewCandidate(),),
        )

        state = _make_state()
        with patch("app.orchestration.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 9, 8, 5, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            _job_hourly(state)

        mock_hourly.assert_called_once()
        mock_daily.assert_called_once_with(state, trigger="hourly_new_candidates")

    @patch("app.orchestration.scheduler._ejecutar_ciclo_diario")
    @patch("app.orchestration.scheduler.run_hourly_cycle")
    @patch("app.orchestration.scheduler.session_scope")
    def test_error_en_hourly_no_mata_scheduler(self, mock_scope, mock_hourly, mock_daily):
        from app.orchestration.scheduler import _job_hourly

        mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        mock_hourly.side_effect = RuntimeError("fallo de ingesta")

        state = _make_state()
        with patch("app.orchestration.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 9, 8, 5, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            _job_hourly(state)

        mock_daily.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# Tests de _ejecutar_ciclo_diario (lock + flujo)
# ═══════════════════════════════════════════════════════════════════════


class TestEjecutarCicloDiario:
    """Tests del ciclo diario con su lock de concurrencia."""

    @pytest.fixture(autouse=True)
    def _presupuesto_disponible(self):
        """Por defecto el tope diario de IA NO está agotado (los tests del
        tope lo sobrescriben con su propio patch)."""
        with patch("app.orchestration.scheduler.verificar_presupuesto_ia", return_value=True) as guardia, \
             patch("app.orchestration.scheduler.verificar_saldo_ia", return_value=True) as saldo, \
             patch("app.orchestration.scheduler.registrar_resumen_ciclo") as resumen, \
             patch("app.orchestration.scheduler.registrar_ciclo_fallido") as fallido:
            self.guardia = guardia
            self.saldo = saldo
            self.resumen = resumen
            self.fallido = fallido
            yield

    @patch("app.orchestration.scheduler._despachar_alertas")
    @patch("app.orchestration.scheduler.run_daily_cycle")
    @patch("app.orchestration.scheduler.calculate_indicators_for_all")
    @patch("app.orchestration.scheduler.backfill_all")
    def test_con_el_tope_diario_agotado_no_inicia_nada_y_avisa(
        self, mock_backfill, mock_indicators, mock_daily, mock_despacho
    ):
        from app.orchestration.scheduler import _daily_cycle_lock, _ejecutar_ciclo_diario

        self.guardia.return_value = False
        state = _make_state()
        state.settings.ai_daily_budget_usd = Decimal("2.00")

        _ejecutar_ciclo_diario(state, trigger="hourly_new_candidates")

        mock_backfill.assert_not_called()
        mock_indicators.assert_not_called()
        mock_daily.assert_not_called()
        mock_despacho.assert_called_once()  # el aviso del tope se envía
        assert self.guardia.call_args.kwargs["tope_usd"] == Decimal("2.00")
        assert self.guardia.call_args.args[0] is state.session_factory
        assert _daily_cycle_lock.acquire(blocking=False), "el lock debe quedar libre tras saltarse el ciclo"
        _daily_cycle_lock.release()

    @patch("app.orchestration.scheduler._despachar_alertas")
    @patch("app.orchestration.scheduler.run_daily_cycle")
    @patch("app.orchestration.scheduler.calculate_indicators_for_all")
    @patch("app.orchestration.scheduler.backfill_all")
    @patch("app.orchestration.scheduler.session_scope")
    def test_con_presupuesto_disponible_corre_y_consulta_el_tope_una_vez(
        self, mock_scope, mock_backfill, mock_indicators, mock_daily, mock_despacho
    ):
        from app.orchestration.scheduler import _ejecutar_ciclo_diario

        mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        mock_backfill.return_value = [_FakeBackfillResult()]
        mock_indicators.return_value = [_FakeIndicatorResult()]
        mock_daily.return_value = _FakeDailyCycleResult()

        _ejecutar_ciclo_diario(_make_state(), trigger="scheduled_daily_0005")

        self.guardia.assert_called_once()
        mock_daily.assert_called_once()

    @patch("app.orchestration.scheduler._despachar_alertas")
    @patch("app.orchestration.scheduler.run_daily_cycle")
    @patch("app.orchestration.scheduler.calculate_indicators_for_all")
    @patch("app.orchestration.scheduler.backfill_all")
    def test_con_saldo_insuficiente_no_inicia_nada_y_avisa(
        self, mock_backfill, mock_indicators, mock_daily, mock_despacho
    ):
        from app.orchestration.scheduler import _daily_cycle_lock, _ejecutar_ciclo_diario

        self.saldo.return_value = False
        state = _make_state()
        state.settings.anthropic_saldo_declarado_usd = Decimal("0.34")
        state.settings.ai_min_balance_per_cycle_usd = Decimal("0.60")

        _ejecutar_ciclo_diario(state, trigger="scheduled_daily_0005")

        mock_backfill.assert_not_called()
        mock_indicators.assert_not_called()
        mock_daily.assert_not_called()
        mock_despacho.assert_called_once()
        kw = self.saldo.call_args.kwargs
        assert kw["saldo_declarado_usd"] == Decimal("0.34") and kw["minimo_por_ciclo_usd"] == Decimal("0.60")
        assert kw["saldo_declarado_en"] is state.settings.anthropic_saldo_declarado_en
        self.resumen.assert_not_called()
        self.fallido.assert_not_called()
        assert _daily_cycle_lock.acquire(blocking=False), "el lock debe quedar libre"
        _daily_cycle_lock.release()

    @patch("app.orchestration.scheduler._despachar_alertas")
    def test_el_saldo_solo_se_revisa_si_el_tope_diario_pasa(self, mock_despacho):
        from app.orchestration.scheduler import _ejecutar_ciclo_diario

        self.guardia.return_value = False
        _ejecutar_ciclo_diario(_make_state(), trigger="scheduled_daily_0005")

        self.saldo.assert_not_called()

    @patch("app.orchestration.scheduler._despachar_alertas")
    @patch("app.orchestration.scheduler.backfill_all")
    def test_el_tope_se_revisa_antes_de_cualquier_trabajo(self, mock_backfill, mock_despacho):
        """Ni siquiera se llama al backfill si el tope está agotado: la
        guardia va primero."""
        from app.orchestration.scheduler import _ejecutar_ciclo_diario

        orden: list[str] = []
        self.guardia.side_effect = lambda *a, **k: orden.append("guardia") or False
        mock_backfill.side_effect = lambda *a, **k: orden.append("backfill")

        _ejecutar_ciclo_diario(_make_state(), trigger="x")

        assert orden == ["guardia"]

    @patch("app.orchestration.scheduler._despachar_alertas")
    @patch("app.orchestration.scheduler.run_daily_cycle")
    @patch("app.orchestration.scheduler.calculate_indicators_for_all")
    @patch("app.orchestration.scheduler.backfill_all")
    @patch("app.orchestration.scheduler.session_scope")
    def test_flujo_completo(
        self, mock_scope, mock_backfill, mock_indicators, mock_daily, mock_despacho
    ):
        from app.orchestration.scheduler import _ejecutar_ciclo_diario

        mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        mock_backfill.return_value = [_FakeBackfillResult()]
        mock_indicators.return_value = [_FakeIndicatorResult()]
        mock_daily.return_value = _FakeDailyCycleResult()

        state = _make_state()
        _ejecutar_ciclo_diario(state, trigger="test")

        mock_backfill.assert_called_once()
        mock_indicators.assert_called_once()
        mock_daily.assert_called_once()
        mock_despacho.assert_called_once()

    @pytest.mark.parametrize(
        ("trigger", "origen"),
        [
            ("scheduled_daily_0005", "PROGRAMADO_DIARIO"),
            ("hourly_new_candidates", "CANDIDATOS_NUEVOS"),
        ],
    )
    @patch("app.orchestration.scheduler._despachar_alertas")
    @patch("app.orchestration.scheduler.run_daily_cycle")
    @patch("app.orchestration.scheduler.calculate_indicators_for_all")
    @patch("app.orchestration.scheduler.backfill_all")
    @patch("app.orchestration.scheduler.session_scope")
    def test_al_terminar_registra_el_resumen_y_luego_despacha(
        self, mock_scope, mock_backfill, mock_indicators, mock_daily, mock_despacho, trigger, origen
    ):
        from app.orchestration.scheduler import _ejecutar_ciclo_diario

        mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        mock_backfill.return_value = [_FakeBackfillResult()]
        mock_indicators.return_value = [_FakeIndicatorResult()]
        resultado = _FakeDailyCycleResult()
        mock_daily.return_value = resultado
        orden: list[str] = []
        self.resumen.side_effect = lambda *a, **k: orden.append("resumen")
        mock_despacho.side_effect = lambda *a, **k: orden.append("despacho")
        state = _make_state()
        state.settings.ai_daily_budget_usd = Decimal("2.00")

        _ejecutar_ciclo_diario(state, trigger=trigger)

        self.resumen.assert_called_once()
        assert self.resumen.call_args.args == (state.session_factory, resultado)
        assert self.resumen.call_args.kwargs["origen"].value == origen
        assert self.resumen.call_args.kwargs["tope_usd"] == Decimal("2.00")
        assert orden == ["resumen", "despacho"], "el resumen debe quedar guardado antes de despachar"
        self.fallido.assert_not_called()

    @patch("app.orchestration.scheduler._despachar_alertas")
    @patch("app.orchestration.scheduler.abrir_entradas_del_ciclo")
    @patch("app.orchestration.scheduler.run_daily_cycle")
    @patch("app.orchestration.scheduler.calculate_indicators_for_all")
    @patch("app.orchestration.scheduler.backfill_all")
    @patch("app.orchestration.scheduler.session_scope")
    def test_despues_del_ciclo_abre_las_posiciones_paper_y_se_las_pasa_al_resumen(
        self, mock_scope, mock_backfill, mock_indicators, mock_daily, mock_abrir, mock_despacho
    ):
        from app.orchestration.scheduler import _ejecutar_ciclo_diario

        mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        mock_backfill.return_value = [_FakeBackfillResult()]
        mock_indicators.return_value = [_FakeIndicatorResult()]
        resultado = _FakeDailyCycleResult()
        mock_daily.return_value = resultado
        apertura = MagicMock(name="apertura")
        mock_abrir.return_value = apertura
        orden: list[str] = []
        mock_daily.side_effect = lambda *a, **k: orden.append("ciclo") or resultado
        mock_abrir.side_effect = lambda *a, **k: orden.append("apertura") or apertura
        self.resumen.side_effect = lambda *a, **k: orden.append("resumen")
        state = _make_state()

        _ejecutar_ciclo_diario(state, trigger="hourly_new_candidates")

        mock_abrir.assert_called_once()
        assert mock_abrir.call_args.args[1] == resultado.ciclo_id
        assert mock_abrir.call_args.kwargs["price_source"] is state.price_source
        assert orden == ["ciclo", "apertura", "resumen"], "la apertura va después del análisis y antes del resumen"
        assert self.resumen.call_args.kwargs["apertura"] is apertura
        assert self.resumen.call_args.kwargs["apertura_fallo"] is False
        self.fallido.assert_not_called()

    @patch("app.orchestration.scheduler._despachar_alertas")
    @patch("app.orchestration.scheduler.abrir_entradas_del_ciclo", side_effect=RuntimeError("binance caído"))
    @patch("app.orchestration.scheduler.run_daily_cycle")
    @patch("app.orchestration.scheduler.calculate_indicators_for_all")
    @patch("app.orchestration.scheduler.backfill_all")
    @patch("app.orchestration.scheduler.session_scope")
    def test_si_la_apertura_falla_el_ciclo_no_se_da_por_fallido_y_el_resumen_lo_dice(
        self, mock_scope, mock_backfill, mock_indicators, mock_daily, mock_abrir, mock_despacho
    ):
        from app.orchestration.scheduler import _ejecutar_ciclo_diario

        mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        mock_backfill.return_value = [_FakeBackfillResult()]
        mock_indicators.return_value = [_FakeIndicatorResult()]
        mock_daily.return_value = _FakeDailyCycleResult()

        _ejecutar_ciclo_diario(_make_state(), trigger="scheduled_daily_0005")

        # el análisis ya quedó guardado: no es un "ciclo fallido", pero el resumen avisa
        self.fallido.assert_not_called()
        self.resumen.assert_called_once()
        assert self.resumen.call_args.kwargs["apertura"] is None
        assert self.resumen.call_args.kwargs["apertura_fallo"] is True
        mock_despacho.assert_called_once()

    @patch("app.orchestration.scheduler._despachar_alertas")
    @patch("app.orchestration.scheduler.backfill_all")
    @patch("app.orchestration.scheduler.session_scope")
    def test_si_el_ciclo_falla_avisa_el_fallo_y_despacha(self, mock_scope, mock_backfill, mock_despacho):
        from app.orchestration.scheduler import _daily_cycle_lock, _ejecutar_ciclo_diario

        mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        error = RuntimeError("Binance offline")
        mock_backfill.side_effect = error
        state = _make_state()

        _ejecutar_ciclo_diario(state, trigger="scheduled_daily_0005")

        self.fallido.assert_called_once()
        assert self.fallido.call_args.args == (state.session_factory,)
        assert self.fallido.call_args.kwargs["error"] is error
        assert self.fallido.call_args.kwargs["origen"].value == "PROGRAMADO_DIARIO"
        mock_despacho.assert_called_once()
        self.resumen.assert_not_called()
        assert _daily_cycle_lock.acquire(blocking=False), "el lock debe quedar libre tras el fallo"
        _daily_cycle_lock.release()

    @patch("app.orchestration.scheduler._despachar_alertas")
    def test_si_el_tope_esta_agotado_no_hay_resumen_ni_aviso_de_fallo(self, mock_despacho):
        from app.orchestration.scheduler import _ejecutar_ciclo_diario

        self.guardia.return_value = False
        _ejecutar_ciclo_diario(_make_state(), trigger="scheduled_daily_0005")

        self.resumen.assert_not_called()
        self.fallido.assert_not_called()

    @patch("app.orchestration.scheduler._despachar_alertas")
    @patch("app.orchestration.scheduler.run_daily_cycle")
    @patch("app.orchestration.scheduler.calculate_indicators_for_all")
    @patch("app.orchestration.scheduler.backfill_all")
    @patch("app.orchestration.scheduler.session_scope")
    def test_lock_impide_ejecucion_concurrente(
        self, mock_scope, mock_backfill, mock_indicators, mock_daily, mock_despacho
    ):
        """Si el lock ya está tomado, la segunda llamada se salta."""
        from app.orchestration.scheduler import _daily_cycle_lock, _ejecutar_ciclo_diario

        mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        mock_backfill.return_value = [_FakeBackfillResult()]
        mock_indicators.return_value = [_FakeIndicatorResult()]
        mock_daily.return_value = _FakeDailyCycleResult()

        state = _make_state()

        # Simular que el lock ya está tomado
        _daily_cycle_lock.acquire(blocking=False)
        try:
            _ejecutar_ciclo_diario(state, trigger="should_skip")
            # No debe haber llamado a backfill porque el lock estaba tomado
            mock_backfill.assert_not_called()
        finally:
            _daily_cycle_lock.release()

    @patch("app.orchestration.scheduler._despachar_alertas")
    @patch("app.orchestration.scheduler.run_daily_cycle")
    @patch("app.orchestration.scheduler.calculate_indicators_for_all")
    @patch("app.orchestration.scheduler.backfill_all")
    @patch("app.orchestration.scheduler.session_scope")
    def test_error_libera_lock(
        self, mock_scope, mock_backfill, mock_indicators, mock_daily, mock_despacho
    ):
        """Un error en cualquier fase libera el lock (finally)."""
        from app.orchestration.scheduler import _daily_cycle_lock, _ejecutar_ciclo_diario

        mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        mock_backfill.side_effect = RuntimeError("Binance offline")

        state = _make_state()
        _ejecutar_ciclo_diario(state, trigger="error_test")

        # El lock debe estar libre tras el error
        acquired = _daily_cycle_lock.acquire(blocking=False)
        assert acquired, "El lock no se liberó tras un error"
        _daily_cycle_lock.release()


# ═══════════════════════════════════════════════════════════════════════
# Tests de _job_heartbeat
# ═══════════════════════════════════════════════════════════════════════


class TestJobHeartbeat:
    """Tests de la escritura del heartbeat."""

    def test_escribe_json_valido(self, tmp_path):
        from app.orchestration.scheduler import _job_heartbeat

        hb_path = tmp_path / ".scheduler_heartbeat.json"
        with patch("app.orchestration.scheduler.HEARTBEAT_PATH", hb_path):
            _job_heartbeat()

        assert hb_path.exists()
        data = json.loads(hb_path.read_text())
        assert data["status"] == "alive"
        assert "timestamp" in data
        assert "pid" in data
        # Validar que el timestamp es ISO 8601
        datetime.fromisoformat(data["timestamp"])

    def test_actualiza_heartbeat_existente(self, tmp_path):
        from app.orchestration.scheduler import _job_heartbeat

        hb_path = tmp_path / ".scheduler_heartbeat.json"
        with patch("app.orchestration.scheduler.HEARTBEAT_PATH", hb_path):
            _job_heartbeat()
            first_data = json.loads(hb_path.read_text())
            _job_heartbeat()
            second_data = json.loads(hb_path.read_text())

        # El segundo heartbeat tiene un timestamp >= el primero
        t1 = datetime.fromisoformat(first_data["timestamp"])
        t2 = datetime.fromisoformat(second_data["timestamp"])
        assert t2 >= t1

    def test_error_de_escritura_no_mata_scheduler(self, tmp_path):
        from app.orchestration.scheduler import _job_heartbeat

        # Directorio inexistente: la escritura fallará
        hb_path = tmp_path / "no_existe" / ".scheduler_heartbeat.json"
        with patch("app.orchestration.scheduler.HEARTBEAT_PATH", hb_path):
            # No debe lanzar
            _job_heartbeat()

        assert not hb_path.exists()

    def test_pinguea_el_deadman_con_la_url_configurada(self, tmp_path):
        from app.orchestration.scheduler import _job_heartbeat

        hb_path = tmp_path / ".scheduler_heartbeat.json"
        with patch("app.orchestration.scheduler.HEARTBEAT_PATH", hb_path), \
             patch("app.orchestration.scheduler.ping_deadman") as ping:
            _job_heartbeat("https://hc-ping.example/token")

        ping.assert_called_once_with("https://hc-ping.example/token")

    def test_sin_url_tambien_llama_a_ping_que_no_hace_nada(self, tmp_path):
        # El job siempre llama a ping_deadman; es este quien ignora None.
        from app.orchestration.scheduler import _job_heartbeat

        hb_path = tmp_path / ".scheduler_heartbeat.json"
        with patch("app.orchestration.scheduler.HEARTBEAT_PATH", hb_path), \
             patch("app.orchestration.scheduler.ping_deadman") as ping:
            _job_heartbeat()

        ping.assert_called_once_with(None)

    def test_el_ping_fallido_no_impide_escribir_el_latido(self, tmp_path):
        from app.orchestration.scheduler import _job_heartbeat

        hb_path = tmp_path / ".scheduler_heartbeat.json"
        with patch("app.orchestration.scheduler.HEARTBEAT_PATH", hb_path), \
             patch("app.orchestration.scheduler.ping_deadman", side_effect=None, return_value=False):
            _job_heartbeat("https://hc-ping.example/token")

        # El archivo se escribió aunque el ping devolviera False
        assert hb_path.exists()


# ═══════════════════════════════════════════════════════════════════════
# Tests de _despachar_alertas
# ═══════════════════════════════════════════════════════════════════════


class TestDespacharAlertas:
    """Tests del despacho de alertas en el scheduler."""

    @patch("app.orchestration.scheduler.dispatch_pending_alerts")
    def test_despacho_exitoso(self, mock_dispatch):
        from app.orchestration.scheduler import _despachar_alertas

        mock_dispatch.return_value = _FakeResultadoDespacho(enviadas=3, fallidas=0)
        state = _make_state(sender=MagicMock())
        now = _utcnow()
        _despachar_alertas(state, now)

        mock_dispatch.assert_called_once_with(
            state.session_factory, state.sender, now=now
        )

    @patch("app.orchestration.scheduler.dispatch_pending_alerts")
    def test_error_no_propaga(self, mock_dispatch):
        from app.orchestration.scheduler import _despachar_alertas

        mock_dispatch.side_effect = ConnectionError("Telegram offline")
        state = _make_state(sender=MagicMock())
        # No debe lanzar
        _despachar_alertas(state, _utcnow())

    @patch("app.orchestration.scheduler.dispatch_pending_alerts")
    def test_sin_alertas_enviadas_no_logea_info(self, mock_dispatch):
        from app.orchestration.scheduler import _despachar_alertas

        mock_dispatch.return_value = _FakeResultadoDespacho(enviadas=0, fallidas=0)
        state = _make_state(sender=MagicMock())
        _despachar_alertas(state, _utcnow())
        # No explota — las 0 alertas no se logean con INFO


# ═══════════════════════════════════════════════════════════════════════
# Tests de _check_db
# ═══════════════════════════════════════════════════════════════════════


class TestCheckDb:
    """Tests del check de conectividad a la base."""

    @patch("app.orchestration.scheduler.session_scope")
    def test_db_responde_devuelve_true(self, mock_scope):
        from app.orchestration.scheduler import _check_db

        mock_session = MagicMock()
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)

        assert _check_db(MagicMock()) is True

    @patch("app.orchestration.scheduler.session_scope")
    def test_db_no_responde_devuelve_false(self, mock_scope):
        from app.orchestration.scheduler import _check_db

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(side_effect=ConnectionError("DB offline"))
        ctx.__exit__ = MagicMock(return_value=False)
        mock_scope.return_value = ctx

        assert _check_db(MagicMock()) is False


# ═══════════════════════════════════════════════════════════════════════
# Tests de _on_job_error
# ═══════════════════════════════════════════════════════════════════════


class TestOnJobError:
    """Tests del listener de errores de APScheduler."""

    def test_logea_sin_explotar(self):
        from app.orchestration.scheduler import _on_job_error

        event = MagicMock()
        event.job_id = "monitor"
        event.exception = RuntimeError("boom")

        # No debe lanzar
        _on_job_error(event)


# ═══════════════════════════════════════════════════════════════════════
# Tests de read_heartbeat (app.services.system)
# ═══════════════════════════════════════════════════════════════════════


class TestReadHeartbeat:
    """Tests de la lectura del heartbeat desde la UI (app.services.system)."""

    def test_heartbeat_valido(self, tmp_path):
        from app.services.system import read_heartbeat

        now = _utcnow()
        ts = (now - timedelta(minutes=2)).isoformat()
        hb_path = tmp_path / ".scheduler_heartbeat.json"
        hb_path.write_text(json.dumps({
            "status": "alive",
            "timestamp": ts,
            "pid": 12345,
        }))

        result = read_heartbeat(hb_path, now=now)
        assert result.disponible is True
        assert result.pid == 12345
        assert result.stale is False
        assert result.edad is not None
        assert result.edad < timedelta(minutes=3)

    def test_heartbeat_viejo(self, tmp_path):
        from app.services.system import read_heartbeat

        now = _utcnow()
        ts = (now - timedelta(minutes=20)).isoformat()
        hb_path = tmp_path / ".scheduler_heartbeat.json"
        hb_path.write_text(json.dumps({
            "status": "alive",
            "timestamp": ts,
            "pid": 12345,
        }))

        result = read_heartbeat(hb_path, now=now)
        assert result.disponible is True
        assert result.stale is True
        assert result.edad > timedelta(minutes=15)

    def test_heartbeat_archivo_no_existe(self, tmp_path):
        from app.services.system import read_heartbeat

        hb_path = tmp_path / "no_existe.json"
        result = read_heartbeat(hb_path, now=_utcnow())
        assert result.disponible is False
        assert result.timestamp is None
        assert result.stale is True

    def test_heartbeat_json_invalido(self, tmp_path):
        from app.services.system import read_heartbeat

        hb_path = tmp_path / ".scheduler_heartbeat.json"
        hb_path.write_text("esto no es json {{{")

        result = read_heartbeat(hb_path, now=_utcnow())
        assert result.disponible is False
        assert result.stale is True

    def test_heartbeat_sin_timestamp(self, tmp_path):
        from app.services.system import read_heartbeat

        hb_path = tmp_path / ".scheduler_heartbeat.json"
        hb_path.write_text(json.dumps({"status": "alive", "pid": 12345}))

        result = read_heartbeat(hb_path, now=_utcnow())
        assert result.disponible is False

    def test_heartbeat_sin_pid(self, tmp_path):
        from app.services.system import read_heartbeat

        now = _utcnow()
        ts = (now - timedelta(seconds=30)).isoformat()
        hb_path = tmp_path / ".scheduler_heartbeat.json"
        hb_path.write_text(json.dumps({"status": "alive", "timestamp": ts}))

        result = read_heartbeat(hb_path, now=now)
        assert result.disponible is True
        assert result.pid is None
        assert result.stale is False

    def test_heartbeat_exactamente_en_umbral(self, tmp_path):
        """Justo en el límite de 15 min: NO es stale (> no >=)."""
        from app.services.system import read_heartbeat

        now = _utcnow()
        ts = (now - timedelta(minutes=15)).isoformat()
        hb_path = tmp_path / ".scheduler_heartbeat.json"
        hb_path.write_text(json.dumps({
            "status": "alive",
            "timestamp": ts,
            "pid": 12345,
        }))

        result = read_heartbeat(hb_path, now=now)
        assert result.disponible is True
        # 15 min exactos: edad es 15 min, no es > 15 min
        assert result.stale is False


# ═══════════════════════════════════════════════════════════════════════
# Tests de _job_daily (wrapper simple)
# ═══════════════════════════════════════════════════════════════════════


class TestJobDaily:
    """Test de que _job_daily delega a _ejecutar_ciclo_diario."""

    @patch("app.orchestration.scheduler._ejecutar_ciclo_diario")
    def test_delega_con_trigger_correcto(self, mock_ejecutar):
        from app.orchestration.scheduler import _job_daily

        state = _make_state()
        _job_daily(state)
        mock_ejecutar.assert_called_once_with(state, trigger="scheduled_daily_0005")
