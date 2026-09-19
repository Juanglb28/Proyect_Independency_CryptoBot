"""Cálculo determinista de indicadores técnicos (sección 6.1 del plan, "Paso 4").

Contrato del plan (sección 16, "CONTRATOS DETERMINISTAS"): estas funciones
deben ser deterministas, testeables e independientes de Streamlit, del LLM
y de Binance. Por eso este módulo no importa SQLAlchemy ni ccxt — solo
recibe listas de `Candle` (el mismo TypedDict de `exchange_client.py`, ya
usado por `candle_ingestion.py`) y devuelve `IndicatorRow`, otra estructura
de datos simple. Leer las velas de la base de datos es responsabilidad de
`app/repositories/candles.py::get_candles`; guardar el resultado es
responsabilidad de `app/repositories/indicators.py`.

Parámetros (decididos en la sección 6.1 del plan, no elegidos aquí):

- RSI: periodo 14, método de Wilder, en las 4 temporalidades.
- ATR: periodo 14, método de Wilder, en las 4 temporalidades.
- Medias móviles 90/120/180: SMA (simple, no EMA), en las 4 temporalidades.
- Volumen relativo: volumen de la vela más reciente ÷ SMA de 30 periodos
  de esa misma temporalidad (no una referencia diaria única).
- Distancia al ATH y al máximo de 90 días: SOLO en velas diarias (`1d`).

Método de Wilder (confirmado contra chartschool.stockcharts.com, la fuente
primaria del indicador, 2026-09-19): la primera media de ganancias/pérdidas
(o de rango verdadero, para ATR) es un promedio simple de los primeros
`period` valores; las siguientes se suavizan de forma recursiva con
`avg = (avg_anterior * (period - 1) + valor_actual) / period`. Este es
exactamente el método que usa TradingView por defecto, que es contra lo que
la Fase 1 del plan exige validar manualmente ("Hecho cuando: tus números
coinciden con TradingView. Si no coinciden, no avanzas.") — ver
`scripts/verify_indicators.py` para ese paso.

Limitación conocida de "distancia al ATH" (documentada, no un bug oculto):
aquí "ATH" significa el máximo dentro de la ventana de velas efectivamente
ingerida (~2 años, sección 3 del plan), no necesariamente el máximo
histórico real desde que el activo empezó a cotizar. Para símbolos con más
de ~2 años de vida (la mayoría de la whitelist), esto puede subestimar la
distancia real al máximo histórico verdadero. Corregirlo exigiría ingerir
más historia o una fuente externa — queda pendiente, no se inventa un valor
para disimularlo (sección P3 del plan: cero datos inventados).

Por qué no pandas/numpy todavía: estas funciones se pueden expresar con
bucles simples de `decimal.Decimal` sobre listas de a lo sumo ~17.500
elementos (2 años en 1h) sin sacrificar claridad ni rendimiento perceptible,
y evitan mezclar `Decimal` (obligatorio aquí para no perder precisión
financiera, sección 15.3 del plan) con los `float64` nativos de pandas, que
requeriría columnas `dtype=object` y anularía buena parte de la ventaja de
usar pandas. Se añadirán si un futuro bloque (backtesting, resampling)
realmente los necesita.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.data.exchange_client import Candle

RSI_PERIOD = 14
ATR_PERIOD = 14
SMA_PERIODS: tuple[int, ...] = (90, 120, 180)
VOLUME_SMA_PERIOD = 30
NINETY_DAY_HIGH_WINDOW = 90

INDICATOR_NAME_RSI = f"RSI_{RSI_PERIOD}"
INDICATOR_NAME_ATR = f"ATR_{ATR_PERIOD}"
INDICATOR_NAME_VOLUMEN_RELATIVO = f"VOLUMEN_RELATIVO_{VOLUME_SMA_PERIOD}"
INDICATOR_NAME_DISTANCIA_ATH = "DISTANCIA_ATH"
INDICATOR_NAME_DISTANCIA_MAX_90D = "DISTANCIA_MAX_90D"

# Timeframe único en el que se calculan distancia al ATH y al máximo de 90
# días (sección 6.1 del plan: "calculadas únicamente en velas diarias").
_DAILY_TIMEFRAME = "1d"


def sma_indicator_name(period: int) -> str:
    return f"SMA_{period}"


@dataclass(frozen=True)
class IndicatorRow:
    """Un valor de indicador ya calculado, listo para persistir.

    `calculated_at` es el `open_time` de la vela sobre la que se calculó —
    nunca "ahora" — para que quede trazable qué sabía el sistema en cada
    instante histórico (sección 15 del plan, look-ahead bias).
    """

    symbol: str
    timeframe: str
    calculated_at: datetime
    indicator_name: str
    value: Decimal


def wilder_rsi(closes: list[Decimal], period: int = RSI_PERIOD) -> list[Decimal | None]:
    """RSI de Wilder. Alineado índice a índice con `closes`.

    Los primeros `period` valores son `None`: no hay suficiente historia
    pasada para calcular siquiera la primera media (no es lo mismo que
    look-ahead bias — aquí simplemente no existen datos pasados
    suficientes, así que declarar el dato como faltante es lo correcto,
    sección P3 del plan).
    """
    n = len(closes)
    result: list[Decimal | None] = [None] * n
    if n <= period:
        return result

    gains = [Decimal(0)] * n
    losses = [Decimal(0)] * n
    for i in range(1, n):
        delta = closes[i] - closes[i - 1]
        if delta > 0:
            gains[i] = delta
        elif delta < 0:
            losses[i] = -delta

    avg_gain = sum(gains[1 : period + 1], Decimal(0)) / period
    avg_loss = sum(losses[1 : period + 1], Decimal(0)) / period
    result[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result[i] = _rsi_from_averages(avg_gain, avg_loss)

    return result


def _rsi_from_averages(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_gain == 0 and avg_loss == 0:
        # Precio completamente plano en la ventana: ni ganancias ni
        # pérdidas. RS queda indefinido (0/0); 50 (neutro) es la
        # convención estándar para este caso degenerado.
        return Decimal(50)
    if avg_loss == 0:
        return Decimal(100)
    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (1 + rs))


def wilder_atr(
    highs: list[Decimal], lows: list[Decimal], closes: list[Decimal], period: int = ATR_PERIOD
) -> list[Decimal | None]:
    """ATR de Wilder. Alineado índice a índice con `highs`/`lows`/`closes`.

    Requiere al menos `period` valores de rango verdadero (TR) para el
    primer promedio, así que el primer resultado no-`None` cae en el
    índice `period - 1` (a diferencia del RSI, el TR del primer día ya es
    calculable sin cierre anterior — ver más abajo).
    """
    n = len(highs)
    result: list[Decimal | None] = [None] * n
    if n < period:
        return result

    true_ranges = [Decimal(0)] * n
    true_ranges[0] = highs[0] - lows[0]  # sin cierre previo: solo el rango del día
    for i in range(1, n):
        true_ranges[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    avg = sum(true_ranges[:period], Decimal(0)) / period
    result[period - 1] = avg

    for i in range(period, n):
        avg = (avg * (period - 1) + true_ranges[i]) / period
        result[i] = avg

    return result


def sma(values: list[Decimal], period: int) -> list[Decimal | None]:
    """Media móvil simple de `period` periodos, incluyendo el valor actual
    en la ventana (igual convención que TradingView para una SMA estándar).
    """
    if period <= 0:
        raise ValueError(f"period debe ser positivo, recibido {period}")

    n = len(values)
    result: list[Decimal | None] = [None] * n
    running_sum = Decimal(0)
    for i in range(n):
        running_sum += values[i]
        if i >= period:
            running_sum -= values[i - period]
        if i >= period - 1:
            result[i] = running_sum / period
    return result


def relative_volume(volumes: list[Decimal], period: int = VOLUME_SMA_PERIOD) -> list[Decimal | None]:
    """Volumen de cada vela ÷ SMA de `period` periodos de ese mismo volumen
    (sección 6.1 del plan). Valor > 1 significa volumen por encima de su
    propia media reciente."""
    volume_sma = sma(volumes, period)
    result: list[Decimal | None] = [None] * len(volumes)
    for i, (volume, avg) in enumerate(zip(volumes, volume_sma)):
        if avg is not None and avg != 0:
            result[i] = volume / avg
    return result


def distance_to_running_high_pct(
    highs: list[Decimal], closes: list[Decimal], window: int | None = None
) -> list[Decimal | None]:
    """Distancia porcentual entre el cierre y el máximo de `highs`.

    `window=None`: máximo expansivo (todo lo visto hasta ese punto — ver la
    limitación documentada arriba sobre qué significa "ATH" aquí).
    `window=N`: máximo móvil de las últimas N velas (p. ej. 90 para el
    máximo de 90 días).

    El resultado es típicamente negativo o cero (el cierre rara vez supera
    el máximo del que forma parte): `(cierre - máximo) / máximo * 100`.
    """
    n = len(highs)
    result: list[Decimal | None] = [None] * n

    if window is None:
        running_max: Decimal | None = None
        for i in range(n):
            running_max = highs[i] if running_max is None else max(running_max, highs[i])
            if running_max != 0:
                result[i] = (closes[i] - running_max) / running_max * Decimal(100)
        return result

    if window <= 0:
        raise ValueError(f"window debe ser positivo, recibido {window}")
    for i in range(window - 1, n):
        window_high = max(highs[i - window + 1 : i + 1])
        if window_high != 0:
            result[i] = (closes[i] - window_high) / window_high * Decimal(100)
    return result


def calculate_indicators(symbol: str, timeframe: str, candles: list[Candle]) -> list[IndicatorRow]:
    """Calcula todos los indicadores de la sección 6.1 para una serie de
    velas de un único símbolo/temporalidad, YA ordenada ascendentemente por
    `open_time` (el orden en que ocurrió el mercado — requisito de la
    sección 15, look-ahead bias).

    No lee ni escribe la base de datos, no decide qué símbolo/temporalidad
    calcular: eso es responsabilidad de quien orquesta (comando de
    `app/main.py` o, más adelante, `app/orchestration/`).
    """
    if not candles:
        return []

    for candle in candles:
        if candle["symbol"] != symbol or candle["timeframe"] != timeframe:
            raise ValueError(
                "Las velas recibidas no corresponden todas al symbol/timeframe pedido "
                f"({symbol}/{timeframe})."
            )

    open_times = [c["open_time"] for c in candles]
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    rows: list[IndicatorRow] = []

    def _append(indicator_name: str, values: list[Decimal | None]) -> None:
        for open_time, value in zip(open_times, values):
            if value is not None:
                rows.append(IndicatorRow(symbol, timeframe, open_time, indicator_name, value))

    _append(INDICATOR_NAME_RSI, wilder_rsi(closes))
    _append(INDICATOR_NAME_ATR, wilder_atr(highs, lows, closes))
    for period in SMA_PERIODS:
        _append(sma_indicator_name(period), sma(closes, period))
    _append(INDICATOR_NAME_VOLUMEN_RELATIVO, relative_volume(volumes))

    if timeframe == _DAILY_TIMEFRAME:
        _append(INDICATOR_NAME_DISTANCIA_ATH, distance_to_running_high_pct(highs, closes, window=None))
        _append(
            INDICATOR_NAME_DISTANCIA_MAX_90D,
            distance_to_running_high_pct(highs, closes, window=NINETY_DAY_HIGH_WINDOW),
        )

    return rows
