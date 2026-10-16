"""Cálculo determinista de resultados de operaciones y métricas (Paso 9;
secciones 8.2 a 8.7 y 15.5 del plan).

Todo aquí es PURO: recibe datos explícitos, devuelve estructuras tipadas.
No lee la base de datos, no llama a Binance, no conoce a ningún LLM ni a
Streamlit (P1). El backtest de la Fase 5 usa exactamente estas funciones
cambiando solo la fuente de datos (decisión del plan, sección 15.5), y las
métricas históricas son reproducibles: mismos datos, mismo resultado.

Contratos del master prompt (sección 16) y cómo se adaptaron:

- `calculate_trade_result(entry, exits, snapshots, contexto, ...)`: las
  comisiones ya viajan dentro de las órdenes ejecutadas (`fee`), así que no
  hay un parámetro `fees` aparte; la slippage es opcional (decisión del
  Paso 8: en paper no se estima, `NULL` cuenta como 0 en el P&L neto).
- `calculate_performance(trades, equity_curve, benchmark, ...)`: el segundo
  argumento es la curva de equity (el drawdown máximo es "una propiedad de
  la trayectoria de capital", sección 8.6), no los snapshots de cada trade;
  MAE/MFE ya vienen dentro de cada `TradeResult`.
- `validate_score_discrimination(trades)`: bootstrap del plan (sección 8.7).

Decisiones del usuario (2026-10-05) que fijan lo que el plan dejaba abierto:

- Una posición con salidas parciales produce UN resultado: `exit_quantity` =
  suma de ventas, `exit_price` = promedio ponderado por cantidad, `exit_fee`
  = suma de comisiones, y `exit_order_id`/`exit_time`/`exit_reason` son los
  de la orden que CERRÓ la posición.
- `thesis_status` se decide por desenlace (`classify_thesis`).
- La expectancy de un tercil del score es el P&L neto medio en USD.

Derivado por mí (el usuario puede corregirlo): `total_return_pct` es el P&L
neto sobre el capital inicial del sistema; `max_drawdown_*` se guardan
NEGATIVOS (o 0), como en la fórmula del plan (`mínimo(drawdown_t)`);
`expectancy_pct` usa la probabilidad de perder = perdedoras / total, de modo
que las operaciones en equilibrio aportan 0 (coincide con la fórmula
literal `1 - win_rate` cuando no hay equilibrios).

Convenciones numéricas como en `risk.py`: `Decimal` siempre, redondeo
`ROUND_HALF_EVEN` a la escala de la columna (precios/cantidades
`NUMERIC(30,12)`, USD `NUMERIC(20,8)`, porcentajes `NUMERIC(12,6)`).
"""

from __future__ import annotations

import random
import statistics
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from enum import Enum
from fractions import Fraction
from math import ceil

from app.domain.enums import EstadoTesis, IntencionOrden, ResultadoOperacion

# --- Constantes de la sección 8.7 del plan --------------------------------
BOOTSTRAP_ITERACIONES = 1000
"""Mínimo del plan ("mínimo 1.000 iteraciones")."""
BOOTSTRAP_SEMILLA = 20261005
"""Semilla fija: el mismo conjunto de operaciones da SIEMPRE el mismo
intervalo (reproducibilidad). La fecha en que se fijó el método."""
NIVEL_DE_CONFIANZA_PCT = 95
MUESTRA_MINIMA_DEL_PLAN = 30
"""Operaciones simuladas que pide el plan (sección 12, Fase 5) para dar una
respuesta de validación."""
MUESTRA_MINIMA_PARA_TERCILES = 3
MUESTRA_MINIMA_POR_ESTRATO = 2
"""Enmienda E4: un mes con una sola operación con score no aporta comparación
dentro de su periodo."""

_ESCALA_PRECIO = Decimal("1E-12")
_ESCALA_USD = Decimal("1E-8")
_ESCALA_PCT = Decimal("1E-6")
_ESCALA_SLIPPAGE_PCT = Decimal("1E-8")
_CIEN = Decimal(100)
_CERO = Decimal(0)


def _precio(valor: Decimal) -> Decimal:
    return valor.quantize(_ESCALA_PRECIO, rounding=ROUND_HALF_EVEN)


def _usd(valor: Decimal) -> Decimal:
    return valor.quantize(_ESCALA_USD, rounding=ROUND_HALF_EVEN)


def _pct(valor: Decimal) -> Decimal:
    return valor.quantize(_ESCALA_PCT, rounding=ROUND_HALF_EVEN)


def _media(valores: Sequence[Decimal]) -> Decimal | None:
    return sum(valores, _CERO) / len(valores) if valores else None


# ======================================================================
# Retornos y drawdown de una serie (benchmark y curva de equity)
# ======================================================================


def calculate_return_pct(price_start: Decimal, price_end: Decimal) -> Decimal:
    """Retorno simple en % entre dos precios (benchmark de la sección 8.4)."""
    if price_start <= 0 or price_end <= 0:
        raise ValueError("los precios deben ser positivos")
    return _pct((price_end - price_start) / price_start * _CIEN)


def average_return_pct(returns: Sequence[Decimal]) -> Decimal:
    """Promedio SIMPLE (no ponderado) de retornos: el
    `market_return_during_trade_pct` de la sección 8.4."""
    if not returns:
        raise ValueError("no hay retornos que promediar")
    return _pct(sum(returns, _CERO) / len(returns))


@dataclass(frozen=True)
class PuntoDeSerie:
    time: datetime
    value: Decimal


@dataclass(frozen=True)
class Drawdown:
    pct: Decimal
    """Mínimo de `(valor - pico) / pico × 100`: negativo, o 0 sin caída."""
    value: Decimal
    """`valor - pico` en el punto de ese mínimo: negativo, o 0."""


def calculate_max_drawdown(series: Sequence[PuntoDeSerie]) -> Drawdown | None:
    """Drawdown máximo de una serie ordenada cronológicamente (sección 8.6):
    `pico_t = max(valor_0..valor_t)`, `drawdown_t = (valor_t - pico_t) /
    pico_t × 100`. `None` si la serie está vacía. Se ordena por tiempo
    (estable) para no depender del orden de entrada."""
    if not series:
        return None
    pico: Decimal | None = None
    peor = Drawdown(pct=_CERO, value=_CERO)
    for punto in sorted(series, key=lambda p: p.time):
        if punto.value <= 0:
            raise ValueError("la serie debe ser positiva para medir un drawdown porcentual")
        pico = punto.value if pico is None else max(pico, punto.value)
        pct = _pct((punto.value - pico) / pico * _CIEN)
        if pct < peor.pct:
            peor = Drawdown(pct=pct, value=_usd(punto.value - pico))
    return peor


@dataclass(frozen=True)
class BenchmarkPeriodo:
    """Referencia temporal equivalente (sección 8.6): retorno y drawdown
    máximo de BTC comprado y mantenido durante el periodo."""

    return_pct: Decimal | None = None
    max_drawdown_pct: Decimal | None = None


def calculate_benchmark(prices: Sequence[PuntoDeSerie]) -> BenchmarkPeriodo:
    """Retorno y drawdown máximo de una serie de precios del periodo. Serie
    vacía o de un solo punto: sin benchmark (`None`), no se inventa."""
    if len(prices) < 2:
        return BenchmarkPeriodo()
    ordenados = sorted(prices, key=lambda p: p.time)
    drawdown = calculate_max_drawdown(ordenados)
    return BenchmarkPeriodo(
        return_pct=calculate_return_pct(ordenados[0].value, ordenados[-1].value),
        max_drawdown_pct=None if drawdown is None else drawdown.pct,
    )


def classify_outcome(net_pnl: Decimal) -> ResultadoOperacion:
    """Ganadora (P&L neto > 0), perdedora (< 0) o equilibrio (= 0): la misma
    regla con la que `calculate_performance` cuenta cada grupo."""
    if net_pnl > 0:
        return ResultadoOperacion.GANADORA
    if net_pnl < 0:
        return ResultadoOperacion.PERDEDORA
    return ResultadoOperacion.EQUILIBRIO


def scale_series_to_base(serie: Sequence[PuntoDeSerie], base: Decimal) -> list[PuntoDeSerie]:
    """Reescala una serie para que su primer punto valga `base` (p. ej. el
    precio de BTC expresado como "qué habría hecho el capital inicial
    comprando y manteniendo BTC"). Solo para comparar visualmente contra la
    curva de equity; no es una métrica nueva. Serie vacía o con primer valor
    cero: lista vacía (no se inventa)."""
    ordenados = sorted(serie, key=lambda p: p.time)
    if not ordenados or ordenados[0].value == 0:
        return []
    primero = ordenados[0].value
    return [PuntoDeSerie(p.time, base * p.value / primero) for p in ordenados]


def serialize_prices(precios: Mapping[str, Decimal]) -> dict[str, str]:
    """Para guardar en JSONB: precios como TEXTO, nunca como float."""
    return {symbol: str(precio) for symbol, precio in precios.items()}


def parse_prices(guardados: Mapping[str, str] | None) -> dict[str, Decimal]:
    return {symbol: Decimal(texto) for symbol, texto in (guardados or {}).items()}


@dataclass(frozen=True)
class BenchmarkDeOperacion:
    """`btc_return_during_trade_pct` y `market_return_during_trade_pct`."""

    btc_return_pct: Decimal | None = None
    market_return_pct: Decimal | None = None


def calculate_trade_benchmark(
    entry_prices: Mapping[str, Decimal],
    exit_prices: Mapping[str, Decimal],
    *,
    universe: Sequence[str],
    btc_symbol: str = "BTCUSDT",
) -> BenchmarkDeOperacion:
    """Benchmark de una operación con los precios OBSERVADOS al abrir y al
    cerrar (decisión del usuario, 2026-10-05: los toma el mismo monitor de 60
    s, sin depender de las velas).

    - BTC: retorno entre su precio de entrada y de salida; `None` si falta
      alguno de los dos.
    - Mercado: promedio SIMPLE de los retornos de TODO el universo (sección
      8.4: "el universo completo"); `None` si falta el precio de cualquier
      par en cualquiera de los dos instantes. Un promedio parcial sería un
      número distinto al que el plan define y no se presenta como si lo fuera.
    """
    btc = None
    if btc_symbol in entry_prices and btc_symbol in exit_prices:
        btc = calculate_return_pct(entry_prices[btc_symbol], exit_prices[btc_symbol])
    mercado = None
    if universe and all(s in entry_prices and s in exit_prices for s in universe):
        mercado = average_return_pct([calculate_return_pct(entry_prices[s], exit_prices[s]) for s in universe])
    return BenchmarkDeOperacion(btc_return_pct=btc, market_return_pct=mercado)


@dataclass(frozen=True)
class MetricasDeSnapshot:
    position_value: Decimal
    unrealized_pnl: Decimal
    unrealized_return_pct: Decimal
    distance_to_stop_pct: Decimal | None
    distance_to_target_pct: Decimal | None
    mae_pct: Decimal
    mfe_pct: Decimal


def calculate_snapshot_metrics(
    *,
    entry_price: Decimal,
    market_price: Decimal,
    quantity: Decimal,
    stop_price: Decimal | None,
    target_price: Decimal | None,
    min_price_seen: Decimal | None,
    max_price_seen: Decimal | None,
) -> MetricasDeSnapshot:
    """Columnas derivadas de un snapshot de trayectoria (ver el docstring de
    `app.db.models.snapshots` para la definición de cada una). Los extremos
    vistos son los de los snapshots ANTERIORES de la posición; el precio de
    este snapshot también cuenta para MAE/MFE."""
    if entry_price <= 0 or market_price <= 0 or quantity <= 0:
        raise ValueError("entry_price, market_price y quantity deben ser positivos")
    minimo = market_price if min_price_seen is None else min(min_price_seen, market_price)
    maximo = market_price if max_price_seen is None else max(max_price_seen, market_price)
    return MetricasDeSnapshot(
        position_value=_usd(quantity * market_price),
        unrealized_pnl=_usd(quantity * (market_price - entry_price)),
        unrealized_return_pct=_pct((market_price / entry_price - 1) * _CIEN),
        distance_to_stop_pct=None if stop_price is None else _pct((market_price - stop_price) / market_price * _CIEN),
        distance_to_target_pct=None
        if target_price is None
        else _pct((target_price - market_price) / market_price * _CIEN),
        mae_pct=_pct((minimo - entry_price) / entry_price * _CIEN),
        mfe_pct=_pct((maximo - entry_price) / entry_price * _CIEN),
    )


# ======================================================================
# Resultado de una operación cerrada (sección 8.4)
# ======================================================================


@dataclass(frozen=True)
class OrdenDeTrade:
    """Una orden `FILLED` de la posición (entrada o salida)."""

    order_id: uuid.UUID
    intencion: IntencionOrden
    filled_at: datetime
    quantity: Decimal
    execution_price: Decimal
    fee: Decimal


@dataclass(frozen=True)
class PuntoDeTrayectoria:
    """Precio observado en un snapshot de la posición."""

    snapshot_time: datetime
    market_price: Decimal


@dataclass(frozen=True)
class ContextoDeEntrada:
    """Lo congelado al decidir/abrir: nada de esto se recalcula después."""

    position_id: uuid.UUID
    symbol: str
    capital_at_entry: Decimal
    initial_stop_price: Decimal
    initial_target_price: Decimal | None = None
    proposal_id: uuid.UUID | None = None
    analysis_cycle_id: uuid.UUID | None = None
    initial_opportunity_score: Decimal | None = None
    initial_risk_score: Decimal | None = None
    initial_confidence_score: Decimal | None = None


@dataclass(frozen=True)
class SlippageEstimada:
    """Solo si algún día se estima o el exchange la entrega (sección 8.4:
    "debe preferirse ese dato sobre una estimación"). En paper es `None`."""

    entry_pct: Decimal | None
    exit_pct: Decimal | None
    total_value: Decimal


@dataclass(frozen=True)
class TradeResult:
    """Resultado definitivo de una operación cerrada. Los nombres son los de
    las columnas de `trade_results`."""

    position_id: uuid.UUID
    entry_order_id: uuid.UUID
    exit_order_id: uuid.UUID
    symbol: str
    side: str
    entry_time: datetime
    exit_time: datetime
    duration_seconds: int
    entry_quantity: Decimal
    exit_quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    capital_at_entry: Decimal
    position_value_at_entry: Decimal
    gross_pnl: Decimal
    gross_return_pct: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    total_fees: Decimal
    estimated_entry_slippage_pct: Decimal | None
    estimated_exit_slippage_pct: Decimal | None
    estimated_total_slippage: Decimal | None
    net_pnl: Decimal
    net_return_pct: Decimal
    initial_stop_price: Decimal | None
    initial_target_price: Decimal | None
    initial_risk_per_unit: Decimal | None
    initial_risk_value: Decimal | None
    realized_r_multiple: Decimal | None
    mae_pct: Decimal | None
    mae_value: Decimal | None
    mfe_pct: Decimal | None
    mfe_value: Decimal | None
    analysis_cycle_id: uuid.UUID | None
    proposal_id: uuid.UUID | None
    initial_opportunity_score: Decimal | None
    initial_risk_score: Decimal | None
    initial_confidence_score: Decimal | None
    thesis_status: EstadoTesis
    exit_reason: IntencionOrden
    btc_return_during_trade_pct: Decimal | None
    market_return_during_trade_pct: Decimal | None


# Salidas que no dicen nada sobre si la tesis se cumplió: las decidió el
# sistema o el usuario por razones ajenas a la tesis.
_SALIDAS_AJENAS_A_LA_TESIS = frozenset(
    {
        IntencionOrden.MANUAL_APPROVAL_EXIT,
        IntencionOrden.SYSTEM_DRAWDOWN,
        IntencionOrden.RECONCILIATION_EXIT,
        IntencionOrden.EMERGENCY_STOP,
        IntencionOrden.OTHER,
    }
)


def classify_thesis(*, exit_reason: IntencionOrden, net_pnl: Decimal, take_profit_hit: bool) -> EstadoTesis:
    """Estado de la tesis por REGLAS FIJAS (decisión del usuario, 2026-10-05),
    sin leer el texto libre de la invalidación ni consultar a un LLM. En
    orden:

    1. cierre por `STOP_LOSS` -> `INVALIDATED`;
    2. salida ajena a la tesis (manual, drawdown del sistema, reconciliación,
       emergencia, otra) -> `INCONCLUSIVE`, gane o pierda;
    3. P&L neto > 0 y el objetivo se alcanzó (hubo TP parcial) ->
       `CONFIRMED`;
    4. P&L neto > 0 sin haber alcanzado el objetivo -> `PARTIALLY_CONFIRMED`;
    5. cualquier otro caso -> `INCONCLUSIVE`.
    """
    if exit_reason == IntencionOrden.STOP_LOSS:
        return EstadoTesis.INVALIDATED
    if exit_reason in _SALIDAS_AJENAS_A_LA_TESIS:
        return EstadoTesis.INCONCLUSIVE
    if net_pnl > 0:
        return EstadoTesis.CONFIRMED if take_profit_hit else EstadoTesis.PARTIALLY_CONFIRMED
    return EstadoTesis.INCONCLUSIVE


def calculate_trade_result(
    entry: OrdenDeTrade,
    exits: Sequence[OrdenDeTrade],
    snapshots: Sequence[PuntoDeTrayectoria],
    contexto: ContextoDeEntrada,
    *,
    slippage: SlippageEstimada | None = None,
    btc_return_pct: Decimal | None = None,
    market_return_pct: Decimal | None = None,
) -> TradeResult:
    """Resultado de una posición CERRADA (fórmulas de la sección 8.4).

    Usa el flujo REAL de órdenes ejecutadas (no asume un único precio de
    salida). Lanza `ValueError` si los datos no describen una posición
    cerrada coherente: sin salidas, salidas que no suman la cantidad de
    entrada, o un orden temporal imposible.
    """
    if entry.intencion != IntencionOrden.ENTRY:
        raise ValueError("`entry` debe ser la orden de entrada (intención ENTRY)")
    if entry.quantity <= 0 or entry.execution_price <= 0:
        raise ValueError("la entrada debe tener cantidad y precio positivos")
    if not exits:
        raise ValueError("la posición no tiene órdenes de salida: no está cerrada")
    if any(s.intencion == IntencionOrden.ENTRY for s in exits):
        raise ValueError("`exits` no puede contener órdenes de entrada")

    salidas = sorted(exits, key=lambda s: s.filled_at)  # estable: respeta el orden dado en empates
    cantidad_salida = sum((s.quantity for s in salidas), _CERO)
    if cantidad_salida != entry.quantity:
        raise ValueError(
            f"las salidas suman {cantidad_salida} pero la entrada fue de {entry.quantity}: la posición no está cerrada"
        )
    cierre = salidas[-1]  # la que dejó la posición en 0
    if salidas[0].filled_at < entry.filled_at:
        raise ValueError("hay una salida anterior a la entrada")

    valor_entrada = _usd(entry.quantity * entry.execution_price)
    ingreso_salidas = sum((s.quantity * s.execution_price for s in salidas), _CERO)
    precio_salida = _precio(ingreso_salidas / cantidad_salida)

    gross_pnl = _usd(ingreso_salidas - entry.quantity * entry.execution_price)
    entry_fee = _usd(entry.fee)
    exit_fee = _usd(sum((s.fee for s in salidas), _CERO))
    total_fees = _usd(entry_fee + exit_fee)
    slippage_total = slippage.total_value if slippage is not None else _CERO
    net_pnl = _usd(gross_pnl - total_fees - slippage_total)

    riesgo_por_unidad = _precio(abs(entry.execution_price - contexto.initial_stop_price))
    riesgo_valor = _usd(riesgo_por_unidad * entry.quantity)
    if riesgo_valor > 0:
        r_multiple: Decimal | None = _pct(net_pnl / riesgo_valor)
    else:
        r_multiple = None  # stop igual a la entrada: el riesgo inicial no está definido

    mae_pct = mae_value = mfe_pct = mfe_value = None
    dentro = [p for p in snapshots if entry.filled_at <= p.snapshot_time <= cierre.filled_at]
    if dentro:
        minimo = min(p.market_price for p in dentro)
        maximo = max(p.market_price for p in dentro)
        mae_pct = _pct((minimo - entry.execution_price) / entry.execution_price * _CIEN)
        mfe_pct = _pct((maximo - entry.execution_price) / entry.execution_price * _CIEN)
        mae_value = _usd((minimo - entry.execution_price) * entry.quantity)
        mfe_value = _usd((maximo - entry.execution_price) * entry.quantity)

    return TradeResult(
        position_id=contexto.position_id,
        entry_order_id=entry.order_id,
        exit_order_id=cierre.order_id,
        symbol=contexto.symbol,
        side="LONG",
        entry_time=entry.filled_at,
        exit_time=cierre.filled_at,
        duration_seconds=int((cierre.filled_at - entry.filled_at).total_seconds()),
        entry_quantity=entry.quantity,
        exit_quantity=cantidad_salida,
        entry_price=entry.execution_price,
        exit_price=precio_salida,
        capital_at_entry=_usd(contexto.capital_at_entry),
        position_value_at_entry=valor_entrada,
        gross_pnl=gross_pnl,
        gross_return_pct=_pct(gross_pnl / valor_entrada * _CIEN),
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        total_fees=total_fees,
        estimated_entry_slippage_pct=None if slippage is None or slippage.entry_pct is None
        else slippage.entry_pct.quantize(_ESCALA_SLIPPAGE_PCT, rounding=ROUND_HALF_EVEN),
        estimated_exit_slippage_pct=None if slippage is None or slippage.exit_pct is None
        else slippage.exit_pct.quantize(_ESCALA_SLIPPAGE_PCT, rounding=ROUND_HALF_EVEN),
        estimated_total_slippage=None if slippage is None else _usd(slippage.total_value),
        net_pnl=net_pnl,
        net_return_pct=_pct(net_pnl / valor_entrada * _CIEN),
        initial_stop_price=contexto.initial_stop_price,
        initial_target_price=contexto.initial_target_price,
        initial_risk_per_unit=riesgo_por_unidad,
        initial_risk_value=riesgo_valor,
        realized_r_multiple=r_multiple,
        mae_pct=mae_pct,
        mae_value=mae_value,
        mfe_pct=mfe_pct,
        mfe_value=mfe_value,
        analysis_cycle_id=contexto.analysis_cycle_id,
        proposal_id=contexto.proposal_id,
        initial_opportunity_score=contexto.initial_opportunity_score,
        initial_risk_score=contexto.initial_risk_score,
        initial_confidence_score=contexto.initial_confidence_score,
        thesis_status=classify_thesis(
            exit_reason=cierre.intencion,
            net_pnl=net_pnl,
            take_profit_hit=any(s.intencion == IntencionOrden.TAKE_PROFIT_PARTIAL for s in salidas),
        ),
        exit_reason=cierre.intencion,
        btc_return_during_trade_pct=btc_return_pct,
        market_return_during_trade_pct=market_return_pct,
    )


# ======================================================================
# Terciles del score (compartidos por performance y validación)
# ======================================================================


@dataclass(frozen=True)
class Terciles:
    """Grupos por score inicial. El tercio medio y el resto (si `n` no es
    múltiplo de 3) quedan fuera de la comparación superior-inferior."""

    superior: tuple[TradeResult, ...]
    medio: tuple[TradeResult, ...]
    inferior: tuple[TradeResult, ...]


def _clave_de_orden(t: TradeResult) -> tuple:
    # Score descendente; empates por fecha de entrada y luego por la
    # posición, para que el orden NUNCA dependa del orden de entrada.
    return (-t.initial_opportunity_score, t.entry_time, str(t.position_id))


def split_terciles(trades: Sequence[TradeResult]) -> Terciles | None:
    """Tercio superior / medio / inferior por `initial_opportunity_score`
    (decisión del usuario, 2026-10-05): `k = n // 3` operaciones en cada
    extremo. Las operaciones sin score no participan. `None` con menos de 3
    operaciones puntuadas."""
    puntuadas = [t for t in trades if t.initial_opportunity_score is not None]
    n = len(puntuadas)
    if n < MUESTRA_MINIMA_PARA_TERCILES:
        return None
    ordenadas = sorted(puntuadas, key=_clave_de_orden)
    k = n // 3
    return Terciles(
        superior=tuple(ordenadas[:k]),
        medio=tuple(ordenadas[k : n - k]),
        inferior=tuple(ordenadas[n - k :]),
    )


def _expectancy_usd(trades: Sequence[TradeResult]) -> Decimal | None:
    media = _media([t.net_pnl for t in trades])
    return None if media is None else _usd(media)


def _win_rate_pct(trades: Sequence[TradeResult]) -> Decimal | None:
    if not trades:
        return None
    ganadoras = sum(1 for t in trades if t.net_pnl > 0)
    return _pct(Decimal(ganadoras) / len(trades) * _CIEN)


def expectancy_usd(trades: Sequence[TradeResult]) -> Decimal | None:
    """P&L neto medio por operación en USD (`None` si no hay operaciones)."""
    return _expectancy_usd(trades)


def win_rate_pct(trades: Sequence[TradeResult]) -> Decimal | None:
    """% de operaciones con P&L neto > 0 (`None` si no hay operaciones)."""
    return _win_rate_pct(trades)


# ======================================================================
# Métricas de rendimiento (sección 8.6)
# ======================================================================


@dataclass(frozen=True)
class PerformanceMetrics:
    """Una fila de `performance_metrics` (derivada y reconstruible)."""

    period_start: datetime
    period_end: datetime
    scope_type: str
    scope_key: str | None
    total_trades: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    win_rate_pct: Decimal | None
    gross_pnl: Decimal
    total_fees: Decimal
    total_slippage: Decimal
    net_pnl: Decimal
    total_return_pct: Decimal | None
    average_trade_return_pct: Decimal | None
    median_trade_return_pct: Decimal | None
    average_winner_pct: Decimal | None
    average_loser_pct: Decimal | None
    largest_win_pct: Decimal | None
    largest_loss_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    max_drawdown_value: Decimal | None
    profit_factor: Decimal | None
    expectancy_per_trade: Decimal | None
    expectancy_pct: Decimal | None
    average_r_multiple: Decimal | None
    average_mae_pct: Decimal | None
    average_mfe_pct: Decimal | None
    average_opportunity_score: Decimal | None
    average_risk_score: Decimal | None
    average_confidence_score: Decimal | None
    high_score_win_rate_pct: Decimal | None
    low_score_win_rate_pct: Decimal | None
    high_score_expectancy: Decimal | None
    low_score_expectancy: Decimal | None
    score_discrimination: Decimal | None
    benchmark_return_pct: Decimal | None
    benchmark_max_drawdown_pct: Decimal | None


def _media_pct(valores: Sequence[Decimal]) -> Decimal | None:
    media = _media(valores)
    return None if media is None else _pct(media)


def calculate_performance(
    trades: Sequence[TradeResult],
    equity_curve: Sequence[PuntoDeSerie],
    benchmark: BenchmarkPeriodo | None,
    *,
    scope_type: str,
    scope_key: str | None,
    period_start: datetime,
    period_end: datetime,
    initial_capital: Decimal,
) -> PerformanceMetrics:
    """Métricas de las operaciones recibidas (el CALLER ya filtró por
    periodo y alcance). Con cero operaciones devuelve ceros y `None` en lo
    que no se puede definir; nunca inventa un número."""
    if period_end < period_start:
        raise ValueError("period_end no puede ser anterior a period_start")
    if initial_capital <= 0:
        raise ValueError("initial_capital debe ser positivo")

    n = len(trades)
    ganadoras = [t for t in trades if t.net_pnl > 0]
    perdedoras = [t for t in trades if t.net_pnl < 0]
    equilibrio = n - len(ganadoras) - len(perdedoras)

    net_pnl = _usd(sum((t.net_pnl for t in trades), _CERO))
    suma_ganancias = sum((t.net_pnl for t in ganadoras), _CERO)
    suma_perdidas = sum((t.net_pnl for t in perdedoras), _CERO)

    promedio_ganadora = _media_pct([t.net_return_pct for t in ganadoras])
    promedio_perdedora = _media_pct([t.net_return_pct for t in perdedoras])
    expectancy_pct: Decimal | None = None
    if n:
        expectancy_pct = _pct(
            Decimal(len(ganadoras)) / n * (promedio_ganadora or _CERO)
            + Decimal(len(perdedoras)) / n * (promedio_perdedora or _CERO)
        )

    drawdown = calculate_max_drawdown(equity_curve)
    terciles = split_terciles(trades)
    alta = _expectancy_usd(terciles.superior) if terciles else None
    baja = _expectancy_usd(terciles.inferior) if terciles else None

    return PerformanceMetrics(
        period_start=period_start,
        period_end=period_end,
        scope_type=scope_type,
        scope_key=scope_key,
        total_trades=n,
        winning_trades=len(ganadoras),
        losing_trades=len(perdedoras),
        breakeven_trades=equilibrio,
        win_rate_pct=_win_rate_pct(trades),
        gross_pnl=_usd(sum((t.gross_pnl for t in trades), _CERO)),
        total_fees=_usd(sum((t.total_fees for t in trades), _CERO)),
        total_slippage=_usd(sum((t.estimated_total_slippage or _CERO for t in trades), _CERO)),
        net_pnl=net_pnl,
        total_return_pct=_pct(net_pnl / initial_capital * _CIEN) if n else None,
        average_trade_return_pct=_media_pct([t.net_return_pct for t in trades]),
        median_trade_return_pct=_pct(statistics.median([t.net_return_pct for t in trades])) if n else None,
        average_winner_pct=promedio_ganadora,
        average_loser_pct=promedio_perdedora,
        largest_win_pct=max((t.net_return_pct for t in ganadoras), default=None),
        largest_loss_pct=min((t.net_return_pct for t in perdedoras), default=None),
        max_drawdown_pct=None if drawdown is None else drawdown.pct,
        max_drawdown_value=None if drawdown is None else drawdown.value,
        # Sin pérdidas el factor no está definido: NULL, no un número inventado.
        profit_factor=_pct(suma_ganancias / abs(suma_perdidas)) if perdedoras else None,
        expectancy_per_trade=_usd(net_pnl / n) if n else None,
        expectancy_pct=expectancy_pct,
        average_r_multiple=_media_pct([t.realized_r_multiple for t in trades if t.realized_r_multiple is not None]),
        average_mae_pct=_media_pct([t.mae_pct for t in trades if t.mae_pct is not None]),
        average_mfe_pct=_media_pct([t.mfe_pct for t in trades if t.mfe_pct is not None]),
        average_opportunity_score=_media_pct(
            [t.initial_opportunity_score for t in trades if t.initial_opportunity_score is not None]
        ),
        average_risk_score=_media_pct([t.initial_risk_score for t in trades if t.initial_risk_score is not None]),
        average_confidence_score=_media_pct(
            [t.initial_confidence_score for t in trades if t.initial_confidence_score is not None]
        ),
        high_score_win_rate_pct=_win_rate_pct(terciles.superior) if terciles else None,
        low_score_win_rate_pct=_win_rate_pct(terciles.inferior) if terciles else None,
        high_score_expectancy=alta,
        low_score_expectancy=baja,
        score_discrimination=_pct(alta - baja) if alta is not None and baja is not None else None,
        benchmark_return_pct=None if benchmark is None else benchmark.return_pct,
        benchmark_max_drawdown_pct=None if benchmark is None else benchmark.max_drawdown_pct,
    )


# ======================================================================
# Validación de la capacidad predictiva (sección 8.7)
# ======================================================================


class EstadoValidacion(str, Enum):
    MUESTRA_INSUFICIENTE = "MUESTRA_INSUFICIENTE"
    """Menos de 3 operaciones con score: no hay terciles; no se calcula."""
    PRELIMINAR = "PRELIMINAR"
    """De 3 a 29: se calcula, pero el plan pide 30 para dar una respuesta."""
    EVALUABLE = "EVALUABLE"
    """30 o más operaciones con score."""


@dataclass(frozen=True)
class ValidationReport:
    status: EstadoValidacion
    n_trades: int
    n_trades_con_score: int
    tercil_size: int | None
    high_score_expectancy: Decimal | None
    low_score_expectancy: Decimal | None
    score_discrimination: Decimal | None
    ci_lower: Decimal | None
    ci_upper: Decimal | None
    discriminates: bool | None
    """`True` si el límite inferior del IC 95% es > 0 (regla del plan);
    `None` si no se pudo calcular. Con `PRELIMINAR` es solo orientativo."""
    iterations: int
    seed: int
    confidence_level_pct: int = NIVEL_DE_CONFIANZA_PCT
    min_trades_plan: int = MUESTRA_MINIMA_DEL_PLAN
    notas: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class BootstrapTerciles:
    """Resultado SIN redondear del método del plan (8.7); cada llamador lo
    cuantiza a la escala de su métrica."""

    k: int
    alta: Decimal
    baja: Decimal
    ci_lower: Decimal
    ci_upper: Decimal


def bootstrap_diferencia_de_terciles(
    valores_por_rango: Sequence[Decimal],
    *,
    iterations: int = BOOTSTRAP_ITERACIONES,
    seed: int = BOOTSTRAP_SEMILLA,
) -> BootstrapTerciles:
    """Núcleo ÚNICO del método de discriminación del plan (8.7), compartido por
    la validación de operaciones reales (`validate_score_discrimination`) y por
    el backtest walk-forward: así no existen dos implementaciones que puedan
    divergir.

    `valores_por_rango[0]` es el resultado de la observación con el score MÁS
    ALTO y el último el del más bajo (el llamador ordena, con su propio
    desempate determinista). Se remuestrea con reemplazo (mismo `n`), se
    recalculan los terciles en cada remuestreo (`k = n // 3`) y el IC 95% usa
    los elementos `ceil(B × 2,5%)` y `ceil(B × 97,5%)` de la lista ordenada.
    """
    if iterations < BOOTSTRAP_ITERACIONES:
        raise ValueError(f"el plan exige al menos {BOOTSTRAP_ITERACIONES} iteraciones")
    n = len(valores_por_rango)
    if n < MUESTRA_MINIMA_PARA_TERCILES:
        raise ValueError("se necesitan al menos 3 observaciones para formar terciles")
    k = n // 3

    alta = sum(valores_por_rango[:k], _CERO) / k
    baja = sum(valores_por_rango[n - k :], _CERO) / k

    rng = random.Random(seed)
    diferencias: list[Decimal] = []
    for _ in range(iterations):
        rangos = sorted(rng.randrange(n) for _ in range(n))
        superior = sum((valores_por_rango[r] for r in rangos[:k]), _CERO) / k
        inferior = sum((valores_por_rango[r] for r in rangos[n - k :]), _CERO) / k
        diferencias.append(superior - inferior)
    diferencias.sort()

    cola = Fraction(100 - NIVEL_DE_CONFIANZA_PCT, 2 * 100)  # 2,5% = 1/40
    idx_inferior = ceil(iterations * cola) - 1
    idx_superior = ceil(iterations * (1 - cola)) - 1
    return BootstrapTerciles(k, alta, baja, diferencias[idx_inferior], diferencias[idx_superior])


def validate_score_discrimination(
    trades: Sequence[TradeResult],
    *,
    iterations: int = BOOTSTRAP_ITERACIONES,
    seed: int = BOOTSTRAP_SEMILLA,
) -> ValidationReport:
    """¿El tercio superior del score rinde mejor que el inferior? (8.7).

    1. `score_discrimination = expectancy(tercil superior) - expectancy(tercil
       inferior)` sobre la muestra, con expectancy = P&L neto medio en USD.
    2. Bootstrap: se remuestrean las operaciones CON reemplazo (mismo `n`),
       se recalculan los terciles dentro de cada remuestreo y se registra la
       diferencia. `iterations >= 1000`, semilla fija.
    3. IC 95% por percentiles de rango más cercano: elementos
       `ceil(B × 2,5%)` y `ceil(B × 97,5%)` de la lista ordenada (base 1).
    4. Discrimina si el límite inferior es > 0.

    Con menos de 3 operaciones puntuadas no hay terciles y el informe lo
    dice (`MUESTRA_INSUFICIENTE`); entre 3 y 29 calcula pero queda marcado
    `PRELIMINAR`.
    """
    if iterations < BOOTSTRAP_ITERACIONES:
        raise ValueError(f"el plan exige al menos {BOOTSTRAP_ITERACIONES} iteraciones")

    puntuadas = [t for t in trades if t.initial_opportunity_score is not None]
    sin_score = len(trades) - len(puntuadas)
    notas: list[str] = []
    if sin_score:
        notas.append(f"{sin_score} operación(es) sin score inicial quedaron fuera de la comparación.")
    ordenadas = sorted(puntuadas, key=_clave_de_orden)  # rango 0 = score más alto
    return _informe_de_valores(
        [t.net_pnl for t in ordenadas], n_trades=len(trades), notas=notas, iterations=iterations, seed=seed
    )


def _informe_de_valores(
    valores_por_rango: Sequence[Decimal],
    *,
    n_trades: int,
    notas: list[str],
    iterations: int,
    seed: int,
) -> ValidationReport:
    """Arma el informe de §8.7 a partir de los resultados ya ordenados por score
    (rango 0 = más alto). Compartido por la medición principal y la estratificada
    (enmienda E4) para que ambas apliquen exactamente el mismo método."""
    n = len(valores_por_rango)
    if n < MUESTRA_MINIMA_PARA_TERCILES:
        notas.append("Menos de 3 operaciones con score: no se pueden formar terciles.")
        return ValidationReport(
            status=EstadoValidacion.MUESTRA_INSUFICIENTE,
            n_trades=n_trades,
            n_trades_con_score=n,
            tercil_size=None,
            high_score_expectancy=None,
            low_score_expectancy=None,
            score_discrimination=None,
            ci_lower=None,
            ci_upper=None,
            discriminates=None,
            iterations=iterations,
            seed=seed,
            notas=tuple(notas),
        )

    boot = bootstrap_diferencia_de_terciles(valores_por_rango, iterations=iterations, seed=seed)
    k = boot.k
    alta = _usd(boot.alta)
    baja = _usd(boot.baja)
    ci_lower = _usd(boot.ci_lower)
    ci_upper = _usd(boot.ci_upper)

    if n < MUESTRA_MINIMA_DEL_PLAN:
        estado = EstadoValidacion.PRELIMINAR
        notas.append(
            f"Preliminar: {n} operaciones con score, por debajo de las {MUESTRA_MINIMA_DEL_PLAN} que pide el plan."
        )
    else:
        estado = EstadoValidacion.EVALUABLE
    if n % 3:
        notas.append(f"{n % 3} operación(es) de rango medio quedaron fuera de ambos terciles (n no es múltiplo de 3).")

    return ValidationReport(
        status=estado,
        n_trades=n_trades,
        n_trades_con_score=n,
        tercil_size=k,
        high_score_expectancy=alta,
        low_score_expectancy=baja,
        score_discrimination=_pct(alta - baja),
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        discriminates=ci_lower > 0,
        iterations=iterations,
        seed=seed,
        notas=tuple(notas),
    )


@dataclass(frozen=True)
class StratifiedValidationReport:
    """Medición ADICIONAL de la enmienda E4 (plan §8.7), junto a la principal.

    `informe` aplica el método de §8.7 a los P&L netos CENTRADOS por mes
    calendario UTC de entrada (valor = `net_pnl` − media del mes). Sus
    `high/low_score_expectancy` y `score_discrimination` son por tanto
    desviaciones respecto del mes, en USD."""

    informe: ValidationReport
    estratos_usados: int
    estratos_excluidos: int
    operaciones_excluidas: int
    """Operaciones con score que cayeron en meses con una sola operación con score."""
    alerta_de_regimen: bool | None
    """`True` si la medición principal dice 'discrimina' y esta no (posible
    efecto de régimen); `False` si ambas coinciden en ese sentido; `None` si
    alguna no se pudo calcular. Es una señal de lectura, no una puerta."""


def _mes_utc(momento: datetime) -> tuple[int, int]:
    utc = momento.astimezone(timezone.utc)
    return (utc.year, utc.month)


def validate_score_discrimination_stratified(
    trades: Sequence[TradeResult],
    *,
    iterations: int = BOOTSTRAP_ITERACIONES,
    seed: int = BOOTSTRAP_SEMILLA,
) -> StratifiedValidationReport:
    """Enmienda E4 (pre-registrada 2026-10-12): mismo método de §8.7 pero sobre
    resultados centrados por mes calendario UTC de entrada, para separar la
    capacidad de discriminar del nivel común del periodo (régimen de mercado).

    Los meses con menos de 2 operaciones con score se excluyen y se cuentan. La
    definición no se ajusta a la vista de resultados."""
    if iterations < BOOTSTRAP_ITERACIONES:
        raise ValueError(f"el plan exige al menos {BOOTSTRAP_ITERACIONES} iteraciones")

    puntuadas = [t for t in trades if t.initial_opportunity_score is not None]
    sin_score = len(trades) - len(puntuadas)
    por_mes: dict[tuple[int, int], list[TradeResult]] = {}
    for t in puntuadas:
        por_mes.setdefault(_mes_utc(t.entry_time), []).append(t)

    centradas: list[tuple[TradeResult, Decimal]] = []
    excluidas = 0
    estratos_excluidos = 0
    for grupo in por_mes.values():
        if len(grupo) < MUESTRA_MINIMA_POR_ESTRATO:
            estratos_excluidos += 1
            excluidas += len(grupo)
            continue
        media = sum((t.net_pnl for t in grupo), _CERO) / len(grupo)
        centradas.extend((t, t.net_pnl - media) for t in grupo)

    notas: list[str] = []
    if sin_score:
        notas.append(f"{sin_score} operación(es) sin score inicial quedaron fuera de la comparación.")
    if estratos_excluidos:
        notas.append(
            f"{estratos_excluidos} mes(es) con una sola operación con score quedaron fuera "
            f"({excluidas} operación(es)): no aportan comparación dentro de su periodo."
        )
    notas.append(
        "Resultados centrados por mes de entrada (enmienda E4). Con pocas operaciones por mes el centrado "
        "correlaciona levemente los valores y el IC puede ser algo optimista; la potencia es baja."
    )

    centradas.sort(key=lambda par: _clave_de_orden(par[0]))
    informe = _informe_de_valores(
        [valor for _, valor in centradas], n_trades=len(trades), notas=notas, iterations=iterations, seed=seed
    )
    return StratifiedValidationReport(
        informe=informe,
        estratos_usados=len(por_mes) - estratos_excluidos,
        estratos_excluidos=estratos_excluidos,
        operaciones_excluidas=excluidas,
        alerta_de_regimen=_alerta_de_regimen(trades, informe, iterations=iterations, seed=seed),
    )


def _alerta_de_regimen(
    trades: Sequence[TradeResult], estratificado: ValidationReport, *, iterations: int, seed: int
) -> bool | None:
    principal = validate_score_discrimination(trades, iterations=iterations, seed=seed)
    if principal.discriminates is None or estratificado.discriminates is None:
        return None
    return principal.discriminates and not estratificado.discriminates
