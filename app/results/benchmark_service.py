"""Benchmark de periodo para `performance_metrics` (Paso 9; sección 8.6).

Es el de BTC comprado y mantenido durante el periodo: retorno y drawdown
máximo sobre los cierres de las velas de 1h YA CERRADAS dentro de
`[period_start, period_end]` (el instante de cierre de una vela es su
`open_time + 1h`; una vela que cierra después de `period_end` no cuenta).

Es una métrica DERIVADA y reconstruible, así que puede recalcularse cuando
llegan velas nuevas (la ingesta es diaria). Con menos de 2 velas en la
ventana no hay benchmark (`None`): no se interpola ni se inventa.

Distinto del benchmark POR OPERACIÓN (`btc_return_during_trade_pct` /
`market_return_during_trade_pct`), que sale de los precios observados al
abrir y cerrar (`app.execution.reference_prices`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.rules.results import BenchmarkPeriodo, PuntoDeSerie, calculate_benchmark
from app.repositories.candles import get_closes_between

BENCHMARK_SYMBOL = "BTCUSDT"
BENCHMARK_TIMEFRAME = "1h"
_DURACION_VELA = timedelta(hours=1)


def calculate_period_benchmark(session: Session, *, period_start: datetime, period_end: datetime) -> BenchmarkPeriodo:
    velas = get_closes_between(
        session,
        BENCHMARK_SYMBOL,
        BENCHMARK_TIMEFRAME,
        open_time_from=period_start - _DURACION_VELA,
        open_time_to=period_end - _DURACION_VELA,
    )
    # `velas.close` es NUMERIC; el modelo ORM lo tipa como float por herencia,
    # así que se normaliza a Decimal vía texto (sin pasar por un float real).
    return calculate_benchmark(
        [PuntoDeSerie(open_time + _DURACION_VELA, Decimal(str(close))) for open_time, close in velas]
    )
