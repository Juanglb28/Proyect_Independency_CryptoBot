"""Diagnóstico de solo lectura para fundamentar la fórmula del pre-filtro
(sección 4.2 / 6.1 / 17 del plan: "la fórmula del pre-filtro permanece
pendiente hasta ver estos valores corriendo con datos reales").

Este script NO decide nada ni escribe en la base de datos. Solo lee los
indicadores y velas ya calculados (Paso 4) para los 20 símbolos de la
whitelist y muestra cómo se ven hoy los tres factores que el plan nombra
explícitamente para el pre-filtro (sección 4.2):

    - tendencia          -> precio actual vs. SMA_90 / SMA_120 / SMA_180
    - volumen relativo   -> VOLUMEN_RELATIVO_30 (ya es un ratio, sin transformar)
    - distancia a rangos -> DISTANCIA_ATH / DISTANCIA_MAX_90D (solo 1d)

Para cada métrica imprime min / p25 / mediana / p75 / max a través de los
20 símbolos, en cada temporalidad donde aplica (1h, 4h, 12h, 1d para
tendencia y volumen; solo 1d para distancia a rangos, igual que se calculan
en `indicator_calculator.py`).

Uso:
    python -m scripts.inspect_prefilter_inputs
"""

from __future__ import annotations

import statistics
from decimal import Decimal

from app.config import get_settings
from app.data.indicator_calculator import (
    INDICATOR_NAME_DISTANCIA_ATH,
    INDICATOR_NAME_DISTANCIA_MAX_90D,
    INDICATOR_NAME_VOLUMEN_RELATIVO,
    SMA_PERIODS,
    sma_indicator_name,
)
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.symbols import ALLOWED_SYMBOLS
from app.logging_config import configure_logging
from app.repositories.candles import get_candles
from app.repositories.indicators import get_indicators

_TIMEFRAMES = ("1h", "4h", "12h", "1d")
_DAILY_TIMEFRAME = "1d"


def _latest_value(rows, indicator_name: str) -> Decimal | None:
    """Último valor (mayor `calculated_at`) de `indicator_name` en `rows`."""
    matching = [r for r in rows if r.indicator_name == indicator_name]
    if not matching:
        return None
    return max(matching, key=lambda r: r.calculated_at).value


def _print_summary(label: str, values: list[float]) -> None:
    if not values:
        print(f"  {label:<28} sin datos (0/{len(ALLOWED_SYMBOLS)} símbolos)")
        return
    values_sorted = sorted(values)
    quantiles = statistics.quantiles(values_sorted, n=4, method="inclusive") if len(values_sorted) >= 2 else [values_sorted[0]] * 3
    print(
        f"  {label:<28} n={len(values):>2}/{len(ALLOWED_SYMBOLS)}  "
        f"min={values_sorted[0]:>8.2f}  p25={quantiles[0]:>8.2f}  "
        f"mediana={quantiles[1]:>8.2f}  p75={quantiles[2]:>8.2f}  max={values_sorted[-1]:>8.2f}"
    )


def main() -> int:
    configure_logging()
    settings = get_settings()
    engine = get_engine(settings)
    session_factory = get_sessionmaker(engine)

    with session_scope(session_factory) as session:
        for timeframe in _TIMEFRAMES:
            print(f"\n=== Temporalidad {timeframe} ===")

            # Tendencia: (último cierre - SMA) / SMA * 100, por cada período de SMA.
            for period in SMA_PERIODS:
                pct_values: list[float] = []
                for symbol in ALLOWED_SYMBOLS:
                    candles = get_candles(session, symbol, timeframe)
                    indicators = get_indicators(session, symbol, timeframe)
                    if not candles:
                        continue
                    sma_value = _latest_value(indicators, sma_indicator_name(period))
                    last_close = candles[-1]["close"]
                    if sma_value is not None and sma_value != 0:
                        pct_values.append(float((last_close - sma_value) / sma_value * 100))
                _print_summary(f"precio vs SMA_{period} (%)", pct_values)

            # Volumen relativo (ya es un ratio; se multiplica x100 solo para
            # que la tabla se lea en la misma escala que los porcentajes).
            vol_values: list[float] = []
            for symbol in ALLOWED_SYMBOLS:
                indicators = get_indicators(session, symbol, timeframe)
                value = _latest_value(indicators, INDICATOR_NAME_VOLUMEN_RELATIVO)
                if value is not None:
                    vol_values.append(float(value))
            _print_summary("volumen relativo (x)", vol_values)

        print(f"\n=== Distancia a rangos (solo {_DAILY_TIMEFRAME}) ===")
        for indicator_name, label in (
            (INDICATOR_NAME_DISTANCIA_ATH, "distancia al ATH (%)"),
            (INDICATOR_NAME_DISTANCIA_MAX_90D, "distancia al máx. 90d (%)"),
        ):
            values: list[float] = []
            for symbol in ALLOWED_SYMBOLS:
                indicators = get_indicators(session, symbol, _DAILY_TIMEFRAME)
                value = _latest_value(indicators, indicator_name)
                if value is not None:
                    values.append(float(value))
            _print_summary(label, values)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
