"""Velas en curso vs. cerradas (decisión 2026-10-08, "A y B"): la vela en
formación se guarda marcada, se refresca hasta cerrar, queda inmutable y nunca
llega a los indicadores ni al análisis."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.data.candle_ingestion import backfill_symbol_timeframe, velas_vencidas_sin_cerrar
from app.data.candle_repair import reparar_serie
from app.data.exchange_client import Candle
from app.data.indicator_calculator import IndicatorRow
from app.data.indicator_pipeline import calculate_indicators_for_symbol_timeframe
from app.db.models.candles import Vela
from app.orchestration.candidate_pipeline import build_datos_analista_cuantitativo
from app.repositories.candles import (
    get_candles,
    get_close_at_or_before,
    get_closes_between,
    get_latest_open_time,
    get_volumes_between,
    list_velas_sin_cerrar,
    save_candles,
    upsert_candles,
)
from app.repositories.indicators import get_indicators, save_indicators

SYM = "BTCUSDT"
T0 = datetime(2025, 3, 1, tzinfo=timezone.utc)
DIA = timedelta(days=1)


def _candle(open_time: datetime, close: str = "100", volume: str = "10", cerrada: bool | None = None, tf: str = "1d") -> Candle:
    c = Candle(
        symbol=SYM,
        timeframe=tf,
        open_time=open_time,
        open=Decimal("100"),
        high=Decimal("120"),
        low=Decimal("90"),
        close=Decimal(close),
        volume=Decimal(volume),
    )
    if cerrada is not None:
        c["cerrada"] = cerrada
    return c


def _fila(db_session, open_time: datetime, tf: str = "1d") -> Vela:
    return db_session.query(Vela).filter_by(symbol=SYM, timeframe=tf, open_time=open_time).one()


def test_una_vela_sin_anotar_se_guarda_como_cerrada(db_session):
    save_candles(db_session, [_candle(T0)])
    assert _fila(db_session, T0).cerrada is True


def test_vela_en_curso_se_refresca_hasta_que_cierra(db_session):
    r1 = upsert_candles(db_session, [_candle(T0, close="100", volume="5", cerrada=False)])
    assert (r1.insertadas, r1.actualizadas, r1.ignoradas) == (1, (), 0)

    r2 = upsert_candles(db_session, [_candle(T0, close="110", volume="8", cerrada=False)])
    assert (r2.insertadas, r2.actualizadas) == (0, (T0,))
    fila = _fila(db_session, T0)
    assert (fila.close, fila.volume, fila.cerrada) == (Decimal("110"), Decimal("8"), False)

    r3 = upsert_candles(db_session, [_candle(T0, close="115", volume="9", cerrada=True)])
    assert r3.actualizadas == (T0,)
    db_session.refresh(fila)
    assert (fila.close, fila.volume, fila.cerrada) == (Decimal("115"), Decimal("9"), True)


def test_vela_cerrada_no_se_modifica_ni_por_el_upsert(db_session):
    upsert_candles(db_session, [_candle(T0, close="115", cerrada=True)])
    r = upsert_candles(db_session, [_candle(T0, close="999", cerrada=True)])
    assert (r.insertadas, r.actualizadas, r.ignoradas) == (0, (), 1)
    assert _fila(db_session, T0).close == Decimal("115")


def test_el_trigger_rechaza_un_update_directo_a_una_vela_cerrada(db_session):
    save_candles(db_session, [_candle(T0)])
    with pytest.raises(DBAPIError, match="inmutable"):
        with db_session.begin_nested():
            db_session.execute(
                text("UPDATE velas SET close = 1 WHERE symbol = :s AND open_time = :t"), {"s": SYM, "t": T0}
            )


def test_el_trigger_permite_actualizar_una_vela_en_curso(db_session):
    upsert_candles(db_session, [_candle(T0, cerrada=False)])
    db_session.execute(text("UPDATE velas SET close = 1 WHERE symbol = :s AND open_time = :t"), {"s": SYM, "t": T0})
    assert _fila(db_session, T0).close == Decimal("1")


def test_las_lecturas_ignoran_la_vela_en_curso(db_session):
    upsert_candles(
        db_session,
        [
            _candle(T0, close="100", cerrada=True),
            _candle(T0 + DIA, close="101", cerrada=True),
            _candle(T0 + 2 * DIA, close="50", volume="1", cerrada=False),
        ],
    )
    assert [c["open_time"] for c in get_candles(db_session, SYM, "1d")] == [T0, T0 + DIA]
    assert len(get_candles(db_session, SYM, "1d", solo_cerradas=False)) == 3
    assert get_latest_open_time(db_session, SYM, "1d") == T0 + DIA
    assert get_close_at_or_before(db_session, SYM, "1d", open_time_at_or_before=T0 + 5 * DIA) == (T0 + DIA, Decimal("101"))
    assert [t for t, _ in get_closes_between(db_session, SYM, "1d", open_time_from=T0, open_time_to=T0 + 5 * DIA)] == [
        T0,
        T0 + DIA,
    ]
    assert set(get_volumes_between(db_session, SYM, "1d", open_time_from=T0, open_time_to=T0 + 5 * DIA)) == {T0, T0 + DIA}
    assert list_velas_sin_cerrar(db_session) == [(SYM, "1d", T0 + 2 * DIA)]


def _fuente(velas_por_llamada: list[list[Candle]]):
    llamadas = iter(velas_por_llamada)

    def fetch(symbol, timeframe, limit, since, exchange=None):
        return next(llamadas)

    return fetch


def test_la_ingesta_marca_la_ultima_como_en_curso_y_luego_la_cierra(db_session):
    ahora1 = T0 + 2 * DIA + timedelta(hours=10)
    primera = [_candle(T0 + i * DIA, close=str(100 + i)) for i in range(3)]
    r1 = backfill_symbol_timeframe(
        db_session, SYM, "1d", T0, fetch_fn=_fuente([primera]), exchange=object(), now=ahora1
    )
    assert (r1.inserted, r1.updated, r1.open_candles) == (3, 0, 1)
    assert [c["open_time"] for c in get_candles(db_session, SYM, "1d")] == [T0, T0 + DIA]

    # Mismo día, más tarde: la vela en curso se refresca (no se congela).
    ahora2 = ahora1 + timedelta(hours=5)
    segunda = [_candle(T0 + i * DIA, close=str(100 + i)) for i in range(2)] + [_candle(T0 + 2 * DIA, close="777")]
    r2 = backfill_symbol_timeframe(
        db_session, SYM, "1d", T0, fetch_fn=_fuente([segunda]), exchange=object(), now=ahora2
    )
    assert (r2.inserted, r2.updated, r2.open_candles) == (0, 1, 1)
    assert _fila(db_session, T0 + 2 * DIA).close == Decimal("777")

    # Al día siguiente la vela ya cerró (y aparece la nueva en curso).
    ahora3 = T0 + 3 * DIA + timedelta(hours=1)
    tercera = [_candle(T0 + 2 * DIA, close="780"), _candle(T0 + 3 * DIA, close="5")]
    r3 = backfill_symbol_timeframe(
        db_session, SYM, "1d", T0 + 2 * DIA, fetch_fn=_fuente([tercera]), exchange=object(), now=ahora3
    )
    assert (r3.inserted, r3.updated, r3.open_candles) == (1, 1, 1)
    assert _fila(db_session, T0 + 2 * DIA).cerrada is True
    assert _fila(db_session, T0 + 2 * DIA).close == Decimal("780")
    assert _fila(db_session, T0 + 3 * DIA).cerrada is False


def test_la_ingesta_descarta_los_indicadores_derivados_de_una_vela_corregida(db_session):
    upsert_candles(db_session, [_candle(T0 + i * DIA) for i in range(3)] + [_candle(T0 + 3 * DIA, cerrada=False)])
    save_indicators(
        db_session,
        [IndicatorRow(SYM, "1d", T0 + i * DIA, "RSI_14", Decimal("50")) for i in range(4)],
    )
    ahora = T0 + 5 * DIA
    backfill_symbol_timeframe(
        db_session,
        SYM,
        "1d",
        T0 + 3 * DIA,
        fetch_fn=_fuente([[_candle(T0 + 3 * DIA, close="123"), _candle(T0 + 4 * DIA)]]),
        exchange=object(),
        now=ahora,
    )
    quedan = {r.calculated_at for r in get_indicators(db_session, SYM, "1d")}
    assert quedan == {T0, T0 + DIA, T0 + 2 * DIA}  # se descartó desde la vela corregida


def test_velas_vencidas_sin_cerrar_excluye_la_vela_en_curso_de_ahora(db_session):
    ahora = T0 + 3 * DIA + timedelta(hours=2)
    upsert_candles(
        db_session,
        [_candle(T0, cerrada=False), _candle(T0 + 3 * DIA, cerrada=False)],
    )
    assert velas_vencidas_sin_cerrar(db_session, ahora=ahora) == [(SYM, "1d", T0)]
    assert velas_vencidas_sin_cerrar(db_session, ahora=ahora, symbols=["ETHUSDT"]) == []


def test_reparar_serie_corrige_velas_congeladas_y_recalcula_indicadores(db_session):
    # Historia sana de 40 velas + una congelada a medias en el medio.
    sanas = [_candle(T0 + i * DIA, close=str(100 + i), volume="10") for i in range(40)]
    sanas[30] = _candle(T0 + 30 * DIA, close="90", volume="1", cerrada=False)  # parcial
    upsert_candles(db_session, sanas)
    calculate_indicators_for_symbol_timeframe(db_session, SYM, "1d")
    # Indicadores viejos calculados sobre la vela parcial: la vela 30 no está
    # en get_candles (no cerrada), así que no hay indicadores de ella; se
    # siembra uno a mano para comprobar que la reparación lo descarta.
    save_indicators(db_session, [IndicatorRow(SYM, "1d", T0 + 30 * DIA, "RSI_14", Decimal("11"))])

    real = [_candle(T0 + i * DIA, close=str(100 + i), volume="10") for i in range(30, 40)]
    real[0] = _candle(T0 + 30 * DIA, close="130", volume="10")
    ahora = T0 + 41 * DIA

    resultado = reparar_serie(db_session, SYM, "1d", ahora=ahora, fetch_fn=_fuente([real]), exchange=object())

    assert resultado is not None
    assert resultado.restantes == 0
    (vela,) = resultado.velas
    assert (vela.close_antes, vela.close_despues) == (Decimal("90"), Decimal("130"))
    assert _fila(db_session, T0 + 30 * DIA).cerrada is True
    assert list_velas_sin_cerrar(db_session) == []
    rsi_30 = [r for r in get_indicators(db_session, SYM, "1d") if r.indicator_name == "RSI_14" and r.calculated_at == T0 + 30 * DIA]
    assert rsi_30 and rsi_30[0].value != Decimal("11")


def test_reparar_serie_sin_velas_vencidas_no_hace_nada(db_session):
    save_candles(db_session, [_candle(T0)])
    assert reparar_serie(db_session, SYM, "1d", ahora=T0 + 9 * DIA, fetch_fn=_fuente([]), exchange=object()) is None


def test_reparar_serie_informa_las_que_binance_no_devolvio(db_session):
    upsert_candles(db_session, [_candle(T0, cerrada=False)])
    resultado = reparar_serie(db_session, SYM, "1d", ahora=T0 + 9 * DIA, fetch_fn=_fuente([[]]), exchange=object())
    assert resultado is not None and resultado.restantes == 1 and resultado.velas[0].close_antes == resultado.velas[0].close_despues


def test_el_analisis_ignora_indicadores_posteriores_a_la_ultima_vela_cerrada(db_session):
    upsert_candles(db_session, [_candle(T0, close="100", tf="1d", cerrada=True), _candle(T0 + DIA, close="1", tf="1d", cerrada=False)])
    save_indicators(
        db_session,
        [
            IndicatorRow(SYM, "1d", T0, "RSI_14", Decimal("60")),
            IndicatorRow(SYM, "1d", T0 + DIA, "RSI_14", Decimal("5")),  # sobre la vela en curso: no vale
        ],
    )
    datos, _ = build_datos_analista_cuantitativo(db_session, SYM)
    bloque = datos["temporalidades"]["1d"]
    assert bloque["precio_cierre_ultima_vela"] == 100.0
    assert bloque["rsi_14"] == 60.0
