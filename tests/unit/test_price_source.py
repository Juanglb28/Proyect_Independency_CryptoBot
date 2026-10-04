"""Tests de `BinanceTickerPriceSource` con un exchange falso (sin red)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.execution.price_source import BinanceTickerPriceSource, PriceUnavailable


class FakeExchange:
    def __init__(self, ticker=None, error: Exception | None = None):
        self.ticker = ticker
        self.error = error
        self.pedidos: list[str] = []

    def fetch_ticker(self, symbol: str):
        self.pedidos.append(symbol)
        if self.error:
            raise self.error
        return self.ticker


def test_usa_el_texto_de_info_last_price_sin_pasar_por_float():
    ex = FakeExchange({"last": 67123.45, "info": {"lastPrice": "67123.45000000"}})
    price = BinanceTickerPriceSource(ex).get_price("BTCUSDT")
    assert price == Decimal("67123.45000000")
    assert ex.pedidos == ["BTC/USDT"]


def test_cae_a_last_si_no_hay_info():
    assert BinanceTickerPriceSource(FakeExchange({"last": 2.5})).get_price("XRPUSDT") == Decimal("2.5")


def test_simbolo_fuera_de_la_whitelist_no_llega_a_binance():
    ex = FakeExchange({"last": 1})
    with pytest.raises(PriceUnavailable):
        BinanceTickerPriceSource(ex).get_price("FOOUSDT")
    assert ex.pedidos == []


def test_error_de_red_se_traduce_a_precio_no_disponible():
    with pytest.raises(PriceUnavailable):
        BinanceTickerPriceSource(FakeExchange(error=TimeoutError("sin red"))).get_price("BTCUSDT")


@pytest.mark.parametrize(
    "ticker",
    [{}, {"last": None}, {"last": 0}, {"last": -5}, {"info": {"lastPrice": "abc"}}, {"info": {"lastPrice": "NaN"}}],
)
def test_precio_ausente_o_invalido_nunca_se_inventa(ticker):
    with pytest.raises(PriceUnavailable):
        BinanceTickerPriceSource(FakeExchange(ticker)).get_price("BTCUSDT")
