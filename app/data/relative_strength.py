"""Fuerza relativa de un símbolo frente a BTC (segunda ronda de
`app/domain/rules/entry_tier.py`).

Se calcula el ratio `close(SÍMBOLO) / close(BTCUSDT)` vela a vela con las
velas USDT que YA existen en la base — no se ingesta ningún par nuevo (p. ej.
LTCBTC) ni se amplía la lista de símbolos autorizados (sección 21 del
master prompt) — y sobre esa serie se aplica el mismo RSI de Wilder del
resto del proyecto (`wilder_rsi`, periodo 14). Es una aproximación del par
cruzado de Binance (que cotiza por su cuenta): puede diferir levemente, y
por eso queda documentado aquí en vez de presentarse como el mismo dato.

Solo se usan velas cuyo `open_time` existe en AMBAS series, nunca se
rellena una vela faltante de un lado (P3). Todo el cálculo es determinista
y usa únicamente las velas ya guardadas al momento del ciclo (sin datos
futuros).
"""

from __future__ import annotations

from decimal import Decimal

from app.data.exchange_client import Candle
from app.data.indicator_calculator import RSI_PERIOD, wilder_rsi


def calculate_ratio_rsi(
    candles_symbol: list[Candle], candles_btc: list[Candle], *, period: int = RSI_PERIOD
) -> Decimal | None:
    """RSI del último punto del ratio SÍMBOLO/BTC, o `None` si no hay
    historia común suficiente (menos de `period + 1` velas en común) o si
    algún cierre de BTC es cero."""
    btc_por_tiempo = {c["open_time"]: c["close"] for c in candles_btc}
    ratios: list[Decimal] = []
    for candle in sorted(candles_symbol, key=lambda c: c["open_time"]):
        cierre_btc = btc_por_tiempo.get(candle["open_time"])
        if cierre_btc is None:
            continue
        if cierre_btc == 0:
            return None
        ratios.append(candle["close"] / cierre_btc)

    if len(ratios) <= period:
        return None
    return wilder_rsi(ratios, period)[-1]
