"""Guardia de velas vencidas de los estudios (2026-10-17).

La definición ESTRICTA (reparación, recuperación, ciclo horario) no cambia; los
estudios usan una variante que ignora lo vencido hace menos de 10 minutos y
solo mira 1d y 1h (lo que leen). Los estudios leen únicamente velas cerradas,
así que una vela recién expirada queda fuera de la serie sin crear un hueco."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.data.candle_ingestion import (
    GRACIA_DE_ESTUDIO,
    TEMPORALIDADES_DE_ESTUDIO,
    velas_vencidas_que_invalidan_un_estudio,
    velas_vencidas_sin_cerrar,
)
from app.data.exchange_client import Candle
from app.domain.rules.candles import GRACIA_DE_CIERRE, hora_de_cierre
from app.repositories.candles import upsert_candles

SYM = "BTCUSDT"
H = datetime(2026, 10, 17, 21, 0, tzinfo=timezone.utc)  # vela de 1h 21:00-22:00
CIERRE = H + timedelta(hours=1)


def _vela(open_time: datetime, tf: str, symbol: str = SYM) -> Candle:
    c = Candle(
        symbol=symbol,
        timeframe=tf,
        open_time=open_time,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("10"),
    )
    c["cerrada"] = False
    return c


def test_los_valores_de_la_decision_son_los_acordados():
    assert TEMPORALIDADES_DE_ESTUDIO == ("1d", "1h")
    assert GRACIA_DE_ESTUDIO == timedelta(minutes=10)


def test_la_definicion_estricta_no_cambia_por_defecto(db_session):
    upsert_candles(db_session, [_vela(H, "1h")])
    ahora = CIERRE + timedelta(minutes=3)
    assert velas_vencidas_sin_cerrar(db_session, ahora=ahora) == [(SYM, "1h", H)]


def test_el_estudio_ignora_lo_vencido_hace_menos_de_la_gracia(db_session):
    upsert_candles(db_session, [_vela(H, "1h")])
    ahora = CIERRE + timedelta(minutes=3)  # ventana :00-:05 del ciclo horario
    assert velas_vencidas_que_invalidan_un_estudio(db_session, ahora=ahora) == []


def test_el_estudio_la_cuenta_pasada_la_gracia(db_session):
    upsert_candles(db_session, [_vela(H, "1h")])
    ahora = CIERRE + timedelta(minutes=20)
    assert velas_vencidas_que_invalidan_un_estudio(db_session, ahora=ahora) == [(SYM, "1h", H)]


def test_limite_exacto_de_la_gracia(db_session):
    upsert_candles(db_session, [_vela(H, "1h")])
    limite = hora_de_cierre(H, "1h") + GRACIA_DE_CIERRE + GRACIA_DE_ESTUDIO
    assert velas_vencidas_que_invalidan_un_estudio(db_session, ahora=limite - timedelta(seconds=1)) == []
    assert velas_vencidas_que_invalidan_un_estudio(db_session, ahora=limite) == [(SYM, "1h", H)]


def test_el_estudio_solo_mira_1d_y_1h(db_session):
    viejo = H - timedelta(days=3)
    upsert_candles(db_session, [_vela(viejo, "4h"), _vela(viejo, "12h"), _vela(viejo, "1d")])
    ahora = CIERRE
    assert velas_vencidas_que_invalidan_un_estudio(db_session, ahora=ahora) == [(SYM, "1d", viejo)]
    # La estricta sigue viendo las tres.
    assert {tf for _, tf, _ in velas_vencidas_sin_cerrar(db_session, ahora=ahora)} == {"4h", "12h", "1d"}


def test_un_parcial_antiguo_en_el_medio_de_la_serie_sigue_bloqueando(db_session):
    upsert_candles(db_session, [_vela(H - timedelta(days=30), "1h")])
    assert velas_vencidas_que_invalidan_un_estudio(db_session, ahora=CIERRE) == [
        (SYM, "1h", H - timedelta(days=30))
    ]


def test_respeta_el_filtro_de_simbolos(db_session):
    upsert_candles(db_session, [_vela(H - timedelta(days=2), "1h")])
    ahora = CIERRE
    assert velas_vencidas_que_invalidan_un_estudio(db_session, ahora=ahora, symbols=["ETHUSDT"]) == []
    assert len(velas_vencidas_que_invalidan_un_estudio(db_session, ahora=ahora, symbols=[SYM])) == 1


def test_gracia_negativa_se_rechaza(db_session):
    with pytest.raises(ValueError):
        velas_vencidas_sin_cerrar(db_session, ahora=CIERRE, gracia=timedelta(seconds=-1))
