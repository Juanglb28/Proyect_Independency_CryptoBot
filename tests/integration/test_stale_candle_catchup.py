"""Recuperación de velas vencidas al arrancar el scheduler (2026-10-17), contra la base real.

La descarga de Binance se sustituye por velas fijas; el resto (consulta de velas
vencidas, reparación, recálculo de indicadores) corre contra PostgreSQL.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import partial
from unittest.mock import MagicMock

from app.data.candle_ingestion import velas_vencidas_sin_cerrar
from app.data.candle_repair import reparar_serie
from app.data.exchange_client import Candle
from app.orchestration import hourly_cycle
from app.orchestration.hourly_cycle import run_stale_candle_catchup
from app.repositories.candles import list_velas_sin_cerrar, upsert_candles

SYM = "BTCUSDT"
T0 = datetime(2025, 3, 1, tzinfo=timezone.utc)
DIA = timedelta(days=1)


def _candle(open_time: datetime, close: str = "100", cerrada: bool | None = None) -> Candle:
    c = Candle(
        symbol=SYM,
        timeframe="1d",
        open_time=open_time,
        open=Decimal("100"),
        high=Decimal("120"),
        low=Decimal("90"),
        close=Decimal(close),
        volume=Decimal("10"),
    )
    if cerrada is not None:
        c["cerrada"] = cerrada
    return c


def _preparar(monkeypatch, db_session, velas_reales):
    """Usa la sesión de la prueba (con rollback al final) en vez de abrir y confirmar la suya."""

    @contextmanager
    def _scope(_factory):
        yield db_session

    monkeypatch.setattr(hourly_cycle, "session_scope", _scope)
    monkeypatch.setattr(hourly_cycle, "get_exchange", lambda: object())
    monkeypatch.setattr(
        hourly_cycle,
        "reparar_serie",
        partial(reparar_serie, fetch_fn=lambda symbol, timeframe, limit, since, exchange=None: list(velas_reales)),
    )


def test_el_catchup_repara_una_vela_congelada_y_deja_la_base_sin_velas_vencidas(db_session, monkeypatch):
    upsert_candles(
        db_session,
        [_candle(T0), _candle(T0 + DIA, close="90", cerrada=False), _candle(T0 + 2 * DIA)],
    )
    ahora = T0 + 5 * DIA
    assert velas_vencidas_sin_cerrar(db_session, ahora=ahora) == [(SYM, "1d", T0 + DIA)]

    _preparar(monkeypatch, db_session, [_candle(T0 + DIA, close="130"), _candle(T0 + 2 * DIA)])
    resultado = run_stale_candle_catchup(MagicMock(), now=ahora)

    assert (resultado.series_con_velas_vencidas, resultado.velas_reparadas) == (1, 1)
    assert (resultado.series_fallidas, resultado.velas_sin_reparar) == (0, 0)
    assert list_velas_sin_cerrar(db_session) == []


def test_el_catchup_no_hace_nada_si_no_hay_velas_vencidas(db_session, monkeypatch):
    upsert_candles(db_session, [_candle(T0), _candle(T0 + DIA)])

    def _no_debe_descargar(*args, **kwargs):
        raise AssertionError("no debía descargar nada")

    _preparar(monkeypatch, db_session, [])
    monkeypatch.setattr(hourly_cycle, "reparar_serie", _no_debe_descargar)
    resultado = run_stale_candle_catchup(MagicMock(), now=T0 + 5 * DIA)
    assert resultado.series_con_velas_vencidas == 0 and resultado.velas_reparadas == 0


def test_el_catchup_informa_las_velas_que_binance_no_devolvio(db_session, monkeypatch):
    upsert_candles(db_session, [_candle(T0, cerrada=False)])
    _preparar(monkeypatch, db_session, [])
    resultado = run_stale_candle_catchup(MagicMock(), now=T0 + 9 * DIA)
    assert resultado.velas_sin_reparar == 1
