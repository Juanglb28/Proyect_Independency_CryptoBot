"""Tests unitarios de app.data.exchange_client.

No hacen ninguna llamada de red: cubren solo las validaciones (whitelist de
símbolos, temporalidades soportadas, conversión de formato) y la
construcción del cliente ccxt. La llamada real a Binance se prueba en
tests/integration/test_exchange_client.py, porque requiere red.
"""

from datetime import datetime, timezone

import pytest

from app.data.exchange_client import (
    InvalidSymbolError,
    InvalidTimeframeError,
    _to_ccxt_symbol,
    fetch_candles,
    get_exchange,
)


def test_get_exchange_has_no_credentials():
    """Este cliente es solo para datos públicos de mercado (sección 15.2 del
    plan: `data` no ejecuta órdenes). No debe cargar API key/secret."""
    exchange = get_exchange()
    assert not exchange.apiKey
    assert not exchange.secret


def test_to_ccxt_symbol_converts_format():
    assert _to_ccxt_symbol("BTCUSDT") == "BTC/USDT"
    assert _to_ccxt_symbol("SHIBUSDT") == "SHIB/USDT"


def test_to_ccxt_symbol_rejects_non_usdt_pair():
    with pytest.raises(InvalidSymbolError):
        _to_ccxt_symbol("BTCEUR")


def test_fetch_candles_rejects_symbol_outside_whitelist():
    """Sección 21 del plan: nunca operar un símbolo fuera de la whitelist
    explícita, sin importar de dónde venga el pedido."""
    with pytest.raises(InvalidSymbolError):
        fetch_candles("PEPEUSDT", "1d", limit=10)


def test_fetch_candles_rejects_unsupported_timeframe():
    """Solo las temporalidades decididas en la sección 6.1 del plan."""
    with pytest.raises(InvalidTimeframeError):
        fetch_candles("BTCUSDT", "1m", limit=10)


def test_fetch_candles_rejects_naive_datetime_for_since():
    """Sección 15.3 del plan: timestamps en UTC explícito, nunca naive."""
    naive = datetime(2024, 1, 1)  # sin tzinfo a propósito
    with pytest.raises(ValueError):
        fetch_candles("BTCUSDT", "1d", limit=10, since=naive)


def test_fetch_candles_accepts_aware_datetime_but_rejects_bad_limit():
    """Confirma que la validación de `limit` corre incluso con un `since`
    válido (no se corta en el primer chequeo por accidente)."""
    aware = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        fetch_candles("BTCUSDT", "1d", limit=0, since=aware)
