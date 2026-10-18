"""Refresco de velas de las 9:30 a. m. de Colombia (14:30 UTC), 2026-10-17.

Solo velas e indicadores: sin pre-filtro, sin candidatos nuevos y sin IA.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.domain.symbols import ALLOWED_SYMBOLS
from app.orchestration import hourly_cycle
from app.orchestration.hourly_cycle import (
    REFRESCO_VELAS_HORA_UTC,
    REFRESCO_VELAS_TEMPORALIDADES,
    run_candle_refresh,
)
from app.orchestration.scheduler import (
    REFRESCO_VELAS_GRACIA_SEGUNDOS,
    _job_refresco_velas,
    _trigger_refresco_velas,
)

AHORA = datetime(2026, 10, 18, 14, 30, 5, tzinfo=timezone.utc)


def _resultado(inserted=0, updated=0):
    r = MagicMock()
    r.inserted = inserted
    r.updated = updated
    return r


def test_la_hora_del_refresco_es_las_9_30_de_colombia():
    # Colombia es UTC-5 todo el año (sin horario de verano)
    assert (REFRESCO_VELAS_HORA_UTC.hour, REFRESCO_VELAS_HORA_UTC.minute) == (14, 30)
    colombia = datetime.combine(AHORA.date(), REFRESCO_VELAS_HORA_UTC, tzinfo=timezone.utc) - timedelta(hours=5)
    assert (colombia.hour, colombia.minute) == (9, 30)


def test_solo_repasa_1h_4h_y_12h_la_diaria_la_cubre_el_ciclo_diario():
    assert REFRESCO_VELAS_TEMPORALIDADES == ("1h", "4h", "12h")


def test_el_disparador_dispara_cada_dia_a_las_1430_utc():
    trigger = _trigger_refresco_velas()
    antes = datetime(2026, 10, 18, 14, 0, tzinfo=timezone.utc)
    siguiente = trigger.get_next_fire_time(None, antes)
    assert siguiente == datetime(2026, 10, 18, 14, 30, tzinfo=timezone.utc)
    despues = trigger.get_next_fire_time(siguiente, siguiente + timedelta(seconds=1))
    assert despues == datetime(2026, 10, 19, 14, 30, tzinfo=timezone.utc)


def test_si_el_equipo_despierta_tarde_el_refresco_aun_corre_dentro_de_3_horas():
    assert REFRESCO_VELAS_GRACIA_SEGUNDOS == 3 * 3600


@patch.object(hourly_cycle, "calculate_indicators_for_all")
@patch.object(hourly_cycle, "backfill_all")
def test_el_refresco_ingesta_y_calcula_indicadores_de_las_tres_temporalidades(mock_backfill, mock_ind):
    mock_backfill.return_value = [_resultado(2, 1), _resultado(3, 0)]
    mock_ind.return_value = [_resultado(5, 0)]
    sesion = MagicMock()

    r = run_candle_refresh(sesion, now=AHORA)

    args, kwargs = mock_backfill.call_args
    assert args[0] is sesion and tuple(args[1]) == tuple(ALLOWED_SYMBOLS) and tuple(args[2]) == ("1h", "4h", "12h")
    assert args[3] == AHORA - timedelta(days=3) and kwargs["until"] == AHORA and kwargs["now"] == AHORA
    ind_args, ind_kwargs = mock_ind.call_args
    assert tuple(ind_args[2]) == ("1h", "4h", "12h") and ind_kwargs["tail"] == hourly_cycle.VELAS_DE_COLA_INCREMENTAL
    assert (r.velas_nuevas, r.velas_actualizadas, r.indicadores_nuevos) == (5, 1, 5)
    assert r.timeframes == ("1h", "4h", "12h")


@patch.object(hourly_cycle, "calculate_indicators_for_all", return_value=[])
@patch.object(hourly_cycle, "backfill_all", return_value=[])
def test_el_refresco_no_toca_el_prefiltro_ni_la_ia(mock_backfill, mock_ind):
    with patch.object(hourly_cycle, "run_prefilter") as prefiltro:
        run_candle_refresh(MagicMock(), now=AHORA)
    prefiltro.assert_not_called()
    assert not hasattr(hourly_cycle.run_candle_refresh, "__wrapped__")
    fuente = hourly_cycle.run_candle_refresh.__code__.co_names
    assert "run_prefilter" not in fuente and "run_daily_cycle" not in fuente


def _state():
    s = MagicMock()
    s.session_factory = MagicMock()
    return s


@patch("app.orchestration.scheduler.run_candle_refresh")
@patch("app.orchestration.scheduler.session_scope")
def test_el_job_corre_el_refresco_en_su_propia_transaccion(mock_scope, mock_refresh):
    mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
    mock_scope.return_value.__exit__ = MagicMock(return_value=False)
    mock_refresh.return_value = MagicMock(
        timeframes=("1h", "4h", "12h"), velas_nuevas=1, velas_actualizadas=2, indicadores_nuevos=3, elapsed_seconds=1.0
    )
    _job_refresco_velas(_state())
    mock_refresh.assert_called_once()


@patch("app.orchestration.scheduler._ejecutar_ciclo_diario")
@patch("app.orchestration.scheduler.run_candle_refresh", side_effect=RuntimeError("Binance caído"))
@patch("app.orchestration.scheduler.session_scope")
def test_un_fallo_del_refresco_no_mata_al_scheduler_ni_dispara_ia(mock_scope, mock_refresh, mock_diario):
    mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
    mock_scope.return_value.__exit__ = MagicMock(return_value=False)
    _job_refresco_velas(_state())  # no lanza
    mock_diario.assert_not_called()


def test_el_scheduler_registra_el_job_del_refresco():
    import inspect

    from app.orchestration import scheduler

    codigo = inspect.getsource(scheduler.start_scheduler)
    assert 'id="candle_refresh"' in codigo and "_trigger_refresco_velas()" in codigo
    assert "misfire_grace_time=REFRESCO_VELAS_GRACIA_SEGUNDOS" in codigo


# ---------------------------------------------------------------------------
# Recuperación de velas vencidas al arrancar (2026-10-17)
# ---------------------------------------------------------------------------

from app.orchestration.hourly_cycle import StaleCandleCatchupResult, run_stale_candle_catchup  # noqa: E402
from app.orchestration.scheduler import (  # noqa: E402
    RECUPERACION_VELAS_GRACIA_SEGUNDOS,
    RECUPERACION_VELAS_RETRASO_SEGUNDOS,
    _job_recuperar_velas_vencidas,
)

T12 = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)


def _factory_falso():
    """Fábrica de sesiones falsa: cada `session_factory()` devuelve una sesión MagicMock."""
    return MagicMock()


def _serie(n_velas, restantes=0):
    r = MagicMock()
    r.velas = tuple(range(n_velas))
    r.restantes = restantes
    return r


@patch.object(hourly_cycle, "get_exchange")
@patch.object(hourly_cycle, "reparar_serie")
@patch.object(hourly_cycle, "velas_vencidas_sin_cerrar", return_value=[])
def test_sin_velas_vencidas_no_descarga_nada(mock_vencidas, mock_reparar, mock_exchange):
    r = run_stale_candle_catchup(_factory_falso(), now=AHORA)
    assert r.series_con_velas_vencidas == 0 and r.velas_reparadas == 0
    mock_reparar.assert_not_called()
    mock_exchange.assert_not_called()


@patch.object(hourly_cycle, "get_exchange")
@patch.object(hourly_cycle, "reparar_serie")
@patch.object(hourly_cycle, "velas_vencidas_sin_cerrar")
def test_repara_cada_serie_una_sola_vez_con_un_unico_cliente(mock_vencidas, mock_reparar, mock_exchange):
    # Dos velas de la misma serie + una de otra: son DOS series, no tres llamadas
    mock_vencidas.return_value = [
        ("BTCUSDT", "12h", T12),
        ("BTCUSDT", "12h", T12 + timedelta(hours=12)),
        ("ETHUSDT", "4h", T12),
    ]
    mock_reparar.side_effect = [_serie(2), _serie(1)]
    r = run_stale_candle_catchup(_factory_falso(), now=AHORA)
    assert r.series_con_velas_vencidas == 2
    assert r.velas_reparadas == 3 and r.series_fallidas == 0 and r.velas_sin_reparar == 0
    assert mock_reparar.call_count == 2
    mock_exchange.assert_called_once()
    llamadas = [(c.args[1], c.args[2]) for c in mock_reparar.call_args_list]
    assert llamadas == [("BTCUSDT", "12h"), ("ETHUSDT", "4h")]
    for c in mock_reparar.call_args_list:
        assert c.kwargs["ahora"] == AHORA


@patch.object(hourly_cycle, "get_exchange")
@patch.object(hourly_cycle, "reparar_serie")
@patch.object(hourly_cycle, "velas_vencidas_sin_cerrar")
def test_un_simbolo_fuera_de_la_lista_autorizada_se_ignora(mock_vencidas, mock_reparar, mock_exchange):
    mock_vencidas.return_value = [("FAKEUSDT", "12h", T12)]
    r = run_stale_candle_catchup(_factory_falso(), now=AHORA)
    assert r.series_con_velas_vencidas == 0
    mock_reparar.assert_not_called()


@patch.object(hourly_cycle, "get_exchange")
@patch.object(hourly_cycle, "reparar_serie")
@patch.object(hourly_cycle, "velas_vencidas_sin_cerrar")
def test_si_una_serie_falla_se_sigue_con_las_demas(mock_vencidas, mock_reparar, mock_exchange):
    mock_vencidas.return_value = [("BTCUSDT", "12h", T12), ("ETHUSDT", "12h", T12), ("SOLUSDT", "12h", T12)]
    mock_reparar.side_effect = [RuntimeError("Binance caído"), _serie(1), _serie(1, restantes=1)]
    r = run_stale_candle_catchup(_factory_falso(), now=AHORA)
    assert r.series_fallidas == 1
    assert r.velas_reparadas == 2
    assert r.velas_sin_reparar == 1
    assert mock_reparar.call_count == 3


def test_la_recuperacion_no_toca_el_prefiltro_ni_la_ia():
    nombres = hourly_cycle.run_stale_candle_catchup.__code__.co_names
    assert "run_prefilter" not in nombres and "run_daily_cycle" not in nombres


@patch("app.orchestration.scheduler.run_stale_candle_catchup")
def test_el_job_de_recuperacion_usa_la_fabrica_de_sesiones_del_estado(mock_catchup):
    mock_catchup.return_value = StaleCandleCatchupResult(2, 5, 0, 0, 1.0)
    estado = _state()
    _job_recuperar_velas_vencidas(estado)
    assert mock_catchup.call_args.args[0] is estado.session_factory


@patch("app.orchestration.scheduler._ejecutar_ciclo_diario")
@patch("app.orchestration.scheduler.run_stale_candle_catchup", side_effect=RuntimeError("base caída"))
def test_un_fallo_de_la_recuperacion_no_mata_al_scheduler_ni_dispara_ia(mock_catchup, mock_diario):
    _job_recuperar_velas_vencidas(_state())  # no lanza
    mock_diario.assert_not_called()


def test_el_scheduler_registra_la_recuperacion_una_sola_vez_al_arrancar():
    import inspect

    from app.orchestration import scheduler

    codigo = inspect.getsource(scheduler.start_scheduler)
    assert 'id="stale_candle_catchup"' in codigo
    assert "DateTrigger(" in codigo  # una sola ejecución, no periódica
    assert "misfire_grace_time=RECUPERACION_VELAS_GRACIA_SEGUNDOS" in codigo
    assert 0 < RECUPERACION_VELAS_RETRASO_SEGUNDOS < RECUPERACION_VELAS_GRACIA_SEGUNDOS
    assert "arrancando 5 jobs" in codigo  # el texto que se busca en los logs
