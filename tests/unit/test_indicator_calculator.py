"""Tests del cálculo determinista de indicadores (sección 6.1 del plan).

Estos tests verifican que el código implementa correctamente el método de
Wilder tal como lo describe la fuente primaria (chartschool.stockcharts.com:
primera media = promedio simple, siguientes = suavizado recursivo). Los
valores esperados están calculados a mano, con la aritmética mostrada en
cada comentario, para que sean auditables sin depender de una fuente
externa. La comparación contra TradingView con datos reales es un paso
aparte y manual (Fase 1 del plan) — ver `scripts/verify_indicators.py`.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.data.exchange_client import Candle
from app.data.indicator_calculator import (
    INDICATOR_NAME_ATR,
    INDICATOR_NAME_DISTANCIA_ATH,
    INDICATOR_NAME_DISTANCIA_MAX_90D,
    INDICATOR_NAME_RSI,
    INDICATOR_NAME_VOLUMEN_RELATIVO,
    calculate_indicators,
    distance_to_running_high_pct,
    relative_volume,
    sma,
    sma_indicator_name,
    wilder_atr,
    wilder_rsi,
)


def D(x) -> Decimal:
    return Decimal(str(x))


def _approx(actual: Decimal | None, expected: float, places: int = 3) -> bool:
    assert actual is not None
    return abs(actual - D(expected)) < D(10) ** -places


# ---------------------------------------------------------------------------
# wilder_rsi
# ---------------------------------------------------------------------------
# closes = [10, 11, 12, 11, 13, 12, 14], period=3
# deltas (índices 1..6):     +1   +1   -1   +2   -1   +2
# ganancias:                  1    1    0    2    0    2
# pérdidas:                   0    0    1    0    1    0
#
# Primera media (deltas índice 1..3): avg_gain=(1+1+0)/3=0.6667
#                                      avg_loss=(0+0+1)/3=0.3333
# RS=2.0 -> RSI[3] = 100 - 100/3 = 66.667
#
# índice 4 (delta índice4: gain=2, loss=0):
#   avg_gain=(0.6667*2+2)/3=1.1111  avg_loss=(0.3333*2+0)/3=0.2222
#   RS=5.0 -> RSI[4]=100-100/6=83.333
#
# índice 5 (delta índice5: gain=0, loss=1):
#   avg_gain=(1.1111*2+0)/3=0.7407  avg_loss=(0.2222*2+1)/3=0.4815
#   RS=1.5385 -> RSI[5]=100-100/2.5385=60.606
#
# índice 6 (delta índice6: gain=2, loss=0):
#   avg_gain=(0.7407*2+2)/3=1.1605  avg_loss=(0.4815*2+0)/3=0.3210
#   RS=3.6154 -> RSI[6]=100-100/4.6154=78.333
def test_wilder_rsi_matches_hand_calculation():
    closes = [D(v) for v in [10, 11, 12, 11, 13, 12, 14]]

    rsi = wilder_rsi(closes, period=3)

    assert rsi[0] is None
    assert rsi[1] is None
    assert rsi[2] is None
    assert _approx(rsi[3], 66.667)
    assert _approx(rsi[4], 83.333)
    assert _approx(rsi[5], 60.606)
    assert _approx(rsi[6], 78.333)


def test_wilder_rsi_returns_all_none_when_not_enough_history():
    closes = [D(v) for v in [10, 11, 12]]
    assert wilder_rsi(closes, period=14) == [None, None, None]


def test_wilder_rsi_flat_prices_is_neutral_50():
    closes = [D(10)] * 10
    rsi = wilder_rsi(closes, period=3)
    assert _approx(rsi[3], 50.0)
    assert _approx(rsi[-1], 50.0)


# ---------------------------------------------------------------------------
# wilder_atr
# ---------------------------------------------------------------------------
# (high, low, close) por día, period=3:
#  1: 10, 8, 9      -> TR = 10-8 = 2 (sin cierre previo)
#  2: 11, 9, 10.5    -> TR = max(2, |11-9|=2, |9-9|=0) = 2
#  3: 12, 10, 11     -> TR = max(2, |12-10.5|=1.5, |10-10.5|=0.5) = 2
#  4: 11, 9.5, 10    -> TR = max(1.5, |11-11|=0, |9.5-11|=1.5) = 1.5
#  5: 13, 10.5, 12.5 -> TR = max(2.5, |13-10|=3, |10.5-10|=0.5) = 3
#  6: 13.5, 12, 13   -> TR = max(1.5, |13.5-12.5|=1, |12-12.5|=0.5) = 1.5
#
# Primera ATR (TR[0..2]=[2,2,2]): avg=2.0 -> índice 2
# índice 3: avg=(2.0*2+1.5)/3=1.8333
# índice 4: avg=(1.8333*2+3)/3=2.2222
# índice 5: avg=(2.2222*2+1.5)/3=1.9815
def test_wilder_atr_matches_hand_calculation():
    highs = [D(v) for v in [10, 11, 12, 11, 13, 13.5]]
    lows = [D(v) for v in [8, 9, 10, 9.5, 10.5, 12]]
    closes = [D(v) for v in [9, 10.5, 11, 10, 12.5, 13]]

    atr = wilder_atr(highs, lows, closes, period=3)

    assert atr[0] is None
    assert atr[1] is None
    assert _approx(atr[2], 2.0)
    assert _approx(atr[3], 1.8333, places=3)
    assert _approx(atr[4], 2.2222, places=3)
    assert _approx(atr[5], 1.9815, places=3)


def test_wilder_atr_returns_all_none_when_not_enough_history():
    highs = [D(v) for v in [10, 11]]
    lows = [D(v) for v in [8, 9]]
    closes = [D(v) for v in [9, 10]]
    assert wilder_atr(highs, lows, closes, period=14) == [None, None]


# ---------------------------------------------------------------------------
# sma
# ---------------------------------------------------------------------------
def test_sma_matches_hand_calculation():
    values = [D(v) for v in [1, 2, 3, 4, 5]]
    result = sma(values, period=3)
    assert result[0] is None
    assert result[1] is None
    assert result[2] == D(2)
    assert result[3] == D(3)
    assert result[4] == D(4)


def test_sma_rejects_non_positive_period():
    import pytest

    with pytest.raises(ValueError):
        sma([D(1)], period=0)


def test_sma_indicator_name():
    assert sma_indicator_name(90) == "SMA_90"


# ---------------------------------------------------------------------------
# relative_volume
# ---------------------------------------------------------------------------
def test_relative_volume_matches_hand_calculation():
    # SMA(3) de [10,10,10,30]: primeros dos None, luego 10, luego (10+10+30)/3=16.667
    volumes = [D(v) for v in [10, 10, 10, 30]]
    result = relative_volume(volumes, period=3)
    assert result[0] is None
    assert result[1] is None
    assert result[2] == D(10) / D(10)  # 1.0
    assert _approx(result[3], 30 / (50 / 3))  # 30 / 16.667 = 1.8


# ---------------------------------------------------------------------------
# distance_to_running_high_pct
# ---------------------------------------------------------------------------
# highs=[10,12,11,15,14], closes=[9,11,10,14,13]
# running_max: 10,12,12,15,15
# distancia: (9-10)/10*100=-10; (11-12)/12*100=-8.333; (10-12)/12*100=-16.667;
#            (14-15)/15*100=-6.667; (13-15)/15*100=-13.333
def test_distance_to_ath_expanding_matches_hand_calculation():
    highs = [D(v) for v in [10, 12, 11, 15, 14]]
    closes = [D(v) for v in [9, 11, 10, 14, 13]]

    result = distance_to_running_high_pct(highs, closes, window=None)

    assert _approx(result[0], -10.0)
    assert _approx(result[1], -8.333)
    assert _approx(result[2], -16.667)
    assert _approx(result[3], -6.667)
    assert _approx(result[4], -13.333)


# highs=[10,12,8,15,9,7], closes=[9,11,7,14,8,6], window=3
# i=2: max(10,12,8)=12 -> (7-12)/12*100=-41.667
# i=3: max(12,8,15)=15 -> (14-15)/15*100=-6.667
# i=4: max(8,15,9)=15 -> (8-15)/15*100=-46.667
# i=5: max(15,9,7)=15 -> (6-15)/15*100=-60.0
def test_distance_to_windowed_high_matches_hand_calculation():
    highs = [D(v) for v in [10, 12, 8, 15, 9, 7]]
    closes = [D(v) for v in [9, 11, 7, 14, 8, 6]]

    result = distance_to_running_high_pct(highs, closes, window=3)

    assert result[0] is None
    assert result[1] is None
    assert _approx(result[2], -41.667)
    assert _approx(result[3], -6.667)
    assert _approx(result[4], -46.667)
    assert _approx(result[5], -60.0)


# ---------------------------------------------------------------------------
# calculate_indicators (orquestación)
# ---------------------------------------------------------------------------
def _make_candles(symbol: str, timeframe: str, n: int, step: timedelta) -> list[Candle]:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles: list[Candle] = []
    price = D(100)
    for i in range(n):
        price = price + D(i % 5) - D(2)  # variación simple, determinista
        candles.append(
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=start + step * i,
                open=price,
                high=price + D(1),
                low=price - D(1),
                close=price,
                volume=D(100 + i),
            )
        )
    return candles


def test_calculate_indicators_includes_daily_only_indicators_for_1d():
    candles = _make_candles("BTCUSDT", "1d", n=200, step=timedelta(days=1))

    rows = calculate_indicators("BTCUSDT", "1d", candles)

    names = {row.indicator_name for row in rows}
    assert INDICATOR_NAME_RSI in names
    assert INDICATOR_NAME_ATR in names
    assert sma_indicator_name(90) in names
    assert INDICATOR_NAME_VOLUMEN_RELATIVO in names
    assert INDICATOR_NAME_DISTANCIA_ATH in names
    assert INDICATOR_NAME_DISTANCIA_MAX_90D in names


def test_calculate_indicators_excludes_daily_only_indicators_for_1h():
    candles = _make_candles("BTCUSDT", "1h", n=200, step=timedelta(hours=1))

    rows = calculate_indicators("BTCUSDT", "1h", candles)

    names = {row.indicator_name for row in rows}
    assert INDICATOR_NAME_RSI in names
    assert INDICATOR_NAME_DISTANCIA_ATH not in names
    assert INDICATOR_NAME_DISTANCIA_MAX_90D not in names


def test_calculate_indicators_empty_input_returns_empty_list():
    assert calculate_indicators("BTCUSDT", "1d", []) == []


def test_calculate_indicators_rejects_mismatched_symbol():
    import pytest

    candles = _make_candles("BTCUSDT", "1d", n=5, step=timedelta(days=1))
    with pytest.raises(ValueError):
        calculate_indicators("ETHUSDT", "1d", candles)


def test_calculate_indicators_rows_reference_the_candle_open_time():
    candles = _make_candles("BTCUSDT", "1d", n=200, step=timedelta(days=1))
    rows = calculate_indicators("BTCUSDT", "1d", candles)

    candle_open_times = {c["open_time"] for c in candles}
    for row in rows:
        assert row.calculated_at in candle_open_times
