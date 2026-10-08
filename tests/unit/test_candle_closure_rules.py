"""Reglas puras de cierre de vela (app.domain.rules.candles) y su anotación en
la ingesta."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.data.candle_ingestion import anotar_cierre
from app.data.exchange_client import Candle
from app.domain.rules.candles import GRACIA_DE_CIERRE, duracion_de_vela, esta_cerrada, hora_de_cierre

T0 = datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("temporalidad", "horas"), [("1h", 1), ("4h", 4), ("12h", 12), ("1d", 24)]
)
def test_hora_de_cierre_es_apertura_mas_duracion(temporalidad, horas):
    assert duracion_de_vela(temporalidad) == timedelta(hours=horas)
    assert hora_de_cierre(T0, temporalidad) == T0 + timedelta(hours=horas)


def test_temporalidad_desconocida_falla_en_vez_de_suponer():
    with pytest.raises(ValueError):
        duracion_de_vela("15m")


def test_instante_sin_zona_horaria_se_rechaza():
    with pytest.raises(ValueError):
        esta_cerrada(T0.replace(tzinfo=None), "1d", ahora=T0)
    with pytest.raises(ValueError):
        esta_cerrada(T0, "1d", ahora=T0.replace(tzinfo=None))


def test_vela_en_formacion_no_esta_cerrada():
    assert not esta_cerrada(T0, "1d", ahora=T0 + timedelta(hours=10))


def test_el_borde_exacto_incluye_la_gracia():
    cierre = hora_de_cierre(T0, "4h")
    assert not esta_cerrada(T0, "4h", ahora=cierre)
    assert not esta_cerrada(T0, "4h", ahora=cierre + GRACIA_DE_CIERRE - timedelta(microseconds=1))
    assert esta_cerrada(T0, "4h", ahora=cierre + GRACIA_DE_CIERRE)


def test_una_vela_posterior_cierra_la_anterior_aunque_el_reloj_diga_otra_cosa():
    assert esta_cerrada(T0, "1d", ahora=T0 + timedelta(hours=1), hay_vela_posterior=True)


def _candle(open_time: datetime, timeframe: str = "1h") -> Candle:
    return Candle(
        symbol="BTCUSDT",
        timeframe=timeframe,
        open_time=open_time,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1"),
    )


def test_anotar_cierre_deja_abierta_solo_la_ultima_en_formacion():
    velas = [_candle(T0 + timedelta(hours=h)) for h in range(3)]
    anotadas = anotar_cierre(velas, ahora=T0 + timedelta(hours=2, minutes=30))
    assert [v["cerrada"] for v in anotadas] == [True, True, False]
    assert "cerrada" not in velas[0]  # no muta la entrada


def test_anotar_cierre_la_ultima_cierra_pasada_su_hora():
    velas = [_candle(T0), _candle(T0 + timedelta(hours=1))]
    anotadas = anotar_cierre(velas, ahora=T0 + timedelta(hours=2, seconds=11))
    assert all(v["cerrada"] for v in anotadas)
