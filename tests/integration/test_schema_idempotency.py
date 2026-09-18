"""Test de integración: la restricción única de `velas` protege la
idempotencia de la ingesta (sección 15.7 del plan: "no duplicar velas
mediante clave única por símbolo, temporalidad y timestamp").

Esto es exactamente el tipo de caso que la sección 15.8 del plan exige
cubrir antes de habilitar cualquier ejecución real: un reintento de ingesta
no debe poder crear una vela duplicada.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.candles import Vela


def _sample_candle(**overrides) -> Vela:
    defaults = dict(
        symbol="BTCUSDT",
        timeframe="1d",
        open_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        open=Decimal("42000.5"),
        high=Decimal("43000.0"),
        low=Decimal("41500.0"),
        close=Decimal("42800.25"),
        volume=Decimal("1234.56789"),
    )
    defaults.update(overrides)
    return Vela(**defaults)


def test_duplicate_candle_is_rejected(db_session):
    """Insertar la misma vela (symbol+timeframe+open_time) dos veces debe
    fallar por la restricción única, no crear un duplicado silencioso."""
    db_session.add(_sample_candle())
    db_session.flush()

    db_session.add(_sample_candle())
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_same_timestamp_different_timeframe_is_allowed(db_session):
    """La misma marca de tiempo en dos temporalidades distintas del mismo
    símbolo NO debe colisionar: la clave única incluye `timeframe`."""
    db_session.add(_sample_candle(timeframe="1d"))
    db_session.add(_sample_candle(timeframe="4h"))
    db_session.flush()  # no debe lanzar
