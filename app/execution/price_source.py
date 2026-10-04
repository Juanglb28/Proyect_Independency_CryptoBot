"""Fuente de precios del motor de ejecución paper (decisión del usuario,
2026-10-04): el ticker PÚBLICO de Binance, sin credenciales.

Un precio es lo que decide a qué valor se abre una posición paper y, más
adelante, cuándo se cruza un stop o un objetivo. Por eso la regla es
estricta: si no hay un precio válido, se lanza `PriceUnavailable` y quien
llama NO ejecuta nada. Nunca se inventa, estima ni reutiliza un precio
viejo (P3 del plan).

Solo se aceptan símbolos de la whitelist (`app.domain.symbols`): ningún
símbolo dinámico puede llegar a Binance desde aquí (sección 21 del master
prompt). No usa API key ni secreto: el cliente es el mismo cliente público
de `app.data.exchange_client.get_exchange`.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Protocol

from app.data.exchange_client import get_exchange
from app.domain.symbols import ALLOWED_SYMBOLS

logger = logging.getLogger(__name__)


class PriceUnavailable(Exception):
    """No se pudo obtener un precio válido para el símbolo pedido."""


class PriceSource(Protocol):
    def get_price(self, symbol: str) -> Decimal:
        """Último precio de `symbol` en USDT, siempre > 0. Lanza
        `PriceUnavailable` si no hay uno válido."""
        ...


class BinanceTickerPriceSource:
    """Precio del último trade según el ticker público de Binance."""

    def __init__(self, exchange=None) -> None:
        self._exchange = exchange if exchange is not None else get_exchange()

    def get_price(self, symbol: str) -> Decimal:
        if symbol not in ALLOWED_SYMBOLS:
            raise PriceUnavailable(f"Símbolo fuera de la whitelist: {symbol!r}")

        try:
            ticker = self._exchange.fetch_ticker(f"{symbol[:-4]}/USDT")
        except Exception as exc:  # ccxt lanza varios tipos de error de red/exchange
            logger.warning(
                "price-source: fallo al pedir el ticker",
                extra={"component": "execution.price_source", "symbol": symbol, "error": type(exc).__name__},
            )
            raise PriceUnavailable(f"No se pudo obtener el ticker de {symbol}: {type(exc).__name__}") from exc

        # Binance devuelve el precio como texto en `info.lastPrice`; se prefiere a
        # `last` (float) para no pasar por un float en un dato financiero.
        raw = (ticker.get("info") or {}).get("lastPrice")
        if raw is None:
            raw = ticker.get("last")
        try:
            price = Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise PriceUnavailable(f"Ticker de {symbol} sin precio utilizable: {raw!r}") from exc

        if not price.is_finite() or price <= 0:
            raise PriceUnavailable(f"Precio no válido para {symbol}: {price}")
        return price
