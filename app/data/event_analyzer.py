"""Cálculo determinista de `analisis_eventos` (sección 6.1/15.10 del plan,
Paso 5): comportamiento del precio de cada símbolo alrededor de cada evento
del catálogo.

Mismo contrato que `indicator_calculator.py` (sección 16, "CONTRATOS
DETERMINISTAS"): funciones puras, sin SQLAlchemy, sin IA, sin Binance.
Reciben velas diarias ya guardadas (mismo `Candle` de `exchange_client.py`)
y un evento, y devuelven `EventAnalysisRow`. Leer eventos/velas de la base
de datos y guardar el resultado es responsabilidad de
`app/data/event_pipeline.py` + `app/repositories/`.

Ventanas (decisión del usuario, 2026-09-29): -7, -1, +1, +3, +7, +30 días
calendario alrededor de la fecha del evento. Las negativas son contexto
previo; las positivas son la reacción posterior que usa el Agente 3 para
juzgar semejanza — ver el docstring de `app/db/models/event_analysis.py`
sobre por qué construir esta tabla no es look-ahead bias.

Regla de precio de referencia: para cada fecha objetivo (la del evento, o
`evento + offset`) se usa el cierre diario más reciente en o ANTES de esa
fecha — nunca uno posterior. Esto evita mezclar direcciones de redondeo
entre eventos y, para las ventanas "antes" del evento (-7, -1), es además
lo que garantiza que nunca se use un dato que en ese instante todavía no
existía.

Si no hay ninguna vela en o antes de la fecha objetivo (el evento es más
antiguo que el historial de velas ingerido, ~2 años — sección 3 del plan),
o si la vela encontrada resulta ser la misma que la de referencia (no hay
ningún dato real distinto dentro de esa ventana — un hueco de datos, no una
ventana de cero cambio), la combinación se declara dato faltante: se omite
la fila en vez de insertar un `0%` engañoso (sección P3 del plan, cero
datos inventados).

BUG REAL encontrado corriendo `scripts/run_agent_demo_full_cycle.py` contra
datos reales (2026-09-30): un evento cuya fecha es reciente puede tener una
ventana futura (`event_date + offset` posterior a la última vela diaria
ingerida — ya sea porque el evento pasó hace pocos días, o porque el
backfill está desactualizado). `_closest_close_on_or_before` no tenía tope
superior, así que devolvía la vela más reciente disponible como si fuera el
precio de esa fecha futura, sin marcarlo como incompleto — dos ventanas
distintas (p. ej. +7 y +30 días) podían terminar con el mismo `window_date`
y el mismo `price_change_pct`, lo cual el propio Agente 3 detectó como una
anomalía en una corrida real. Decisión del usuario (2026-09-30): una
ventana cuya fecha objetivo todavía no tiene ninguna vela real ingerida se
omite (dato faltante), nunca se sustituye por la vela más reciente
disponible — ver el chequeo `target > ultima_vela_disponible` en
`calculate_event_analysis`. Volver a correr
`scripts/calculate_event_analysis.py` más adelante, cuando el historial de
velas ya cubra esa fecha, sí calculará la fila.
"""

from __future__ import annotations

import bisect
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.data.exchange_client import Candle
from app.domain.symbols import ALLOWED_SYMBOLS

WINDOW_OFFSETS_DAYS: tuple[int, ...] = (-7, -1, 1, 3, 7, 30)

_DAILY_TIMEFRAME = "1d"
_GLOBAL_SCOPE = "GLOBAL"


@dataclass(frozen=True)
class EventAnalysisRow:
    """Una fila de `analisis_eventos`, lista para persistir."""

    evento_id: uuid.UUID
    symbol: str
    window_offset_days: int
    reference_date: date
    reference_price: Decimal
    window_date: date
    window_price: Decimal
    price_change_pct: Decimal


def resolve_scope_symbols(scope: str | None) -> tuple[str, ...]:
    """Interpreta el campo `eventos.scope` (texto libre, ver docstring de
    `app/db/models/events.py`).

    `None`, vacío, o "GLOBAL" (sin distinguir mayúsculas): el evento afecta
    a todo el universo de la whitelist. En otro caso, una lista de símbolos
    separados por coma (p. ej. "ETHUSDT" o "BTCUSDT,ETHUSDT"). Un símbolo
    que no esté en `ALLOWED_SYMBOLS` es un error de curación del catálogo
    —typo o símbolo fuera de la whitelist— y se rechaza explícitamente en
    vez de ignorarlo en silencio (sección 21 del plan: lista de símbolos
    explícita y controlada).
    """
    if scope is None or scope.strip() == "" or scope.strip().upper() == _GLOBAL_SCOPE:
        return ALLOWED_SYMBOLS

    symbols = tuple(s.strip().upper() for s in scope.split(","))
    invalid = [s for s in symbols if s not in ALLOWED_SYMBOLS]
    if invalid:
        raise ValueError(
            f"scope de evento contiene símbolos no autorizados: {invalid}. "
            f"Deben estar en ALLOWED_SYMBOLS (app/domain/symbols.py) o el campo debe ser 'GLOBAL'."
        )
    return symbols


def _closes_by_date(symbol: str, daily_candles: list[Candle]) -> dict[date, Decimal]:
    result: dict[date, Decimal] = {}
    for candle in daily_candles:
        if candle["symbol"] != symbol or candle["timeframe"] != _DAILY_TIMEFRAME:
            raise ValueError(
                f"Las velas recibidas no son todas diarias de {symbol}: "
                f"encontrado symbol={candle['symbol']!r} timeframe={candle['timeframe']!r}."
            )
        result[candle["open_time"].date()] = candle["close"]
    return result


def _closest_close_on_or_before(
    sorted_dates: list[date], closes: dict[date, Decimal], target: date
) -> tuple[date, Decimal] | None:
    """La vela diaria más reciente con fecha <= `target`, o `None` si no
    existe ninguna (el historial de velas no llega tan atrás)."""
    idx = bisect.bisect_right(sorted_dates, target) - 1
    if idx < 0:
        return None
    found_date = sorted_dates[idx]
    return found_date, closes[found_date]


def calculate_event_analysis(
    evento_id: uuid.UUID, event_date: date, symbol: str, daily_candles: list[Candle]
) -> list[EventAnalysisRow]:
    """Calcula las filas de `analisis_eventos` de un evento para un símbolo.

    `daily_candles` debe ser la serie completa de velas diarias guardadas de
    `symbol` (no hace falta pre-filtrarla por fecha: esta función busca el
    subconjunto relevante). Devuelve como máximo `len(WINDOW_OFFSETS_DAYS)`
    filas: menos si el historial de velas no cubre alguna de las ventanas
    (dato faltante, no un error — ver docstring del módulo).
    """
    closes = _closes_by_date(symbol, daily_candles)
    if not closes:
        return []

    sorted_dates = sorted(closes)

    reference = _closest_close_on_or_before(sorted_dates, closes, event_date)
    if reference is None:
        return []
    reference_date, reference_price = reference
    if reference_price == 0:
        return []

    ultima_vela_disponible = sorted_dates[-1]

    rows: list[EventAnalysisRow] = []
    for offset in WINDOW_OFFSETS_DAYS:
        target = event_date + timedelta(days=offset)
        if target > ultima_vela_disponible:
            # La ventana todavía no ocurrió en el historial de velas
            # ingerido (evento reciente, o backfill desactualizado) — ver
            # el docstring del módulo. Nunca se reutiliza la vela más
            # reciente disponible como si fuera el precio de esta fecha
            # futura.
            continue
        found = _closest_close_on_or_before(sorted_dates, closes, target)
        if found is None:
            continue
        window_date, window_price = found
        if window_date == reference_date:
            # Ningún dato real distinto dentro de esta ventana (hueco de
            # historial) — no un 0% de cambio genuino.
            continue

        price_change_pct = (window_price - reference_price) / reference_price * Decimal(100)
        rows.append(
            EventAnalysisRow(
                evento_id=evento_id,
                symbol=symbol,
                window_offset_days=offset,
                reference_date=reference_date,
                reference_price=reference_price,
                window_date=window_date,
                window_price=window_price,
                price_change_pct=price_change_pct,
            )
        )

    return rows
