"""Dashboard: vista ejecutiva (sección 8.9 del plan). Solo lectura.

Capital, equity, drawdown actual, métricas globales del historial completo,
y las curvas de equity, drawdown y del benchmark (BTC comprado y mantenido,
reescalado para que arranque en el capital inicial).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.rules.results import PerformanceMetrics, PuntoDeSerie, scale_series_to_base
from app.domain.rules.risk import calculate_drawdown_pct
from app.execution.emergency_stop import EmergencyStop
from app.execution.system_control import get_system_state
from app.repositories.candles import get_closes_between
from app.repositories.equity import list_observaciones
from app.results.benchmark_service import BENCHMARK_SYMBOL, BENCHMARK_TIMEFRAME
from app.results.performance_service import compute_performance
from app.services._periodo import inicio_del_historial
from app.services._series import downsample

MAX_PUNTOS_CURVA = 600
_DURACION_VELA = timedelta(hours=1)


@dataclass(frozen=True)
class DashboardView:
    generado_en: datetime
    capital_inicial: Decimal
    periodo_inicio: datetime
    equity_actual: Decimal | None
    pico_equity: Decimal
    drawdown_actual_pct: Decimal | None
    sistema_pausado: bool
    parada_emergencia: bool
    posiciones_abiertas: int
    ultima_observacion_en: datetime | None
    metricas: PerformanceMetrics
    curva_equity: tuple[PuntoDeSerie, ...]
    curva_drawdown: tuple[PuntoDeSerie, ...]
    curva_benchmark_btc: tuple[PuntoDeSerie, ...]


def get_dashboard(
    session: Session,
    *,
    initial_capital: Decimal,
    now: datetime,
    emergency_stop: EmergencyStop | None = None,
    max_puntos: int = MAX_PUNTOS_CURVA,
) -> DashboardView:
    estado = get_system_state(session, initial_capital=initial_capital, emergency_stop=emergency_stop)
    observaciones = list_observaciones(session)

    inicio = inicio_del_historial(session, now=now)
    metricas = compute_performance(session, period_start=inicio, period_end=now, initial_capital=initial_capital)[0]

    curva_equity = [PuntoDeSerie(o.observed_at, o.equity_usd) for o in observaciones]
    curva_drawdown = [
        PuntoDeSerie(o.observed_at, calculate_drawdown_pct(o.equity_usd, o.peak_equity_usd)) for o in observaciones
    ]

    velas = get_closes_between(
        session,
        BENCHMARK_SYMBOL,
        BENCHMARK_TIMEFRAME,
        open_time_from=inicio - _DURACION_VELA,
        open_time_to=now - _DURACION_VELA,
    )
    btc = [PuntoDeSerie(t + _DURACION_VELA, Decimal(str(close))) for t, close in velas]

    drawdown_actual = (
        None if estado.ultimo_equity is None else calculate_drawdown_pct(estado.ultimo_equity, estado.pico_vigente)
    )
    return DashboardView(
        generado_en=now,
        capital_inicial=initial_capital,
        periodo_inicio=inicio,
        equity_actual=estado.ultimo_equity,
        pico_equity=estado.pico_vigente,
        drawdown_actual_pct=drawdown_actual,
        sistema_pausado=estado.pausado,
        parada_emergencia=estado.parada_emergencia,
        posiciones_abiertas=estado.posiciones_abiertas,
        ultima_observacion_en=estado.ultima_observacion_en,
        metricas=metricas,
        curva_equity=downsample(curva_equity, max_puntos),
        curva_drawdown=downsample(curva_drawdown, max_puntos, conservar="minimo"),
        curva_benchmark_btc=downsample(scale_series_to_base(btc, initial_capital), max_puntos),
    )
