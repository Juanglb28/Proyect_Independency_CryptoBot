"""Test de integración: descarga real de velas desde Binance mainnet.

Requiere acceso saliente a api.binance.com (no requiere API key: el
endpoint de velas es público). Es la categoría "integración" de la
sección 15.8 del plan porque depende de un sistema externo real, igual
que los tests de base de datos dependen de PostgreSQL real.

Nota para quien corra esto en un entorno con la red restringida (por
ejemplo, un sandbox con egress limitado a un allowlist): este test
fallará por timeout/conexión rechazada, no por un defecto del código.
Ver README para el detalle de esa limitación conocida.
"""

from datetime import datetime, timezone

from app.data.exchange_client import fetch_candles


def test_fetch_candles_returns_real_recent_btc_data():
    """Fase 0 del plan, criterio de aceptación literal: "ves velas reales
    en tu terminal y sabes qué hace cada línea del script"."""
    candles = fetch_candles("BTCUSDT", "1d", limit=5)

    assert len(candles) == 5
    for candle in candles:
        assert candle["symbol"] == "BTCUSDT"
        assert candle["timeframe"] == "1d"
        assert candle["open_time"].tzinfo is not None
        # Un precio de BTC en cero o negativo indica un parseo roto, no un
        # mercado real; > 1 USD es una cota absurdamente laxa a propósito,
        # solo para detectar un bug de parseo, no para validar el precio.
        assert candle["open"] > 0
        assert candle["high"] >= candle["low"] > 0
        assert candle["volume"] >= 0

    # Las velas deben venir en orden cronológico ascendente, sin huecos
    # negativos (una vela no puede "empezar" antes que la anterior).
    for earlier, later in zip(candles, candles[1:]):
        assert earlier["open_time"] < later["open_time"]


def test_fetch_candles_since_filters_to_requested_window():
    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles = fetch_candles("ETHUSDT", "1d", limit=3, since=since)

    assert len(candles) == 3
    for candle in candles:
        assert candle["open_time"] >= since
