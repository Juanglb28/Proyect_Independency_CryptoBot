"""Performance: métricas agregadas por periodo y alcance (secciones 8.6/8.9).

Se calculan al pedirlas, con las mismas funciones puras que usa
`performance_metrics` (`compute_performance`), SIN escribir en la tabla: la
interfaz es de solo lectura. Si quieres dejar el cálculo guardado, existe el
comando `python -m scripts.results performance`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.enums import AlcanceMetricas, SerieOperacion
from app.domain.rules.results import PerformanceMetrics
from app.results.performance_service import compute_performance
from app.services._periodo import inicio_de_periodo


@dataclass(frozen=True)
class PerformanceView:
    generado_en: datetime
    periodo_inicio: datetime
    periodo_fin: datetime
    dias: int | None
    serie: SerieOperacion | None
    global_: PerformanceMetrics
    por_simbolo: tuple[PerformanceMetrics, ...]
    por_tercil: tuple[PerformanceMetrics, ...]


def get_performance_view(
    session: Session,
    *,
    initial_capital: Decimal,
    now: datetime,
    days: int | None = None,
    serie: SerieOperacion | None = SerieOperacion.ESTRATEGIA,
) -> PerformanceView:
    """`serie`: estrategia (por defecto), observación o `None` (conjunta). Solo la
    de estrategia tiene curva de equity: en las otras el drawdown máximo es n/d."""
    inicio = inicio_de_periodo(session, now=now, days=days, serie=serie)
    metricas = compute_performance(
        session, period_start=inicio, period_end=now, initial_capital=initial_capital, serie=serie
    )
    return PerformanceView(
        generado_en=now,
        periodo_inicio=inicio,
        periodo_fin=now,
        dias=days,
        serie=serie,
        global_=metricas[0],
        por_simbolo=tuple(m for m in metricas if m.scope_type == AlcanceMetricas.SYMBOL.value),
        por_tercil=tuple(m for m in metricas if m.scope_type == AlcanceMetricas.SCORE_TERCIL.value),
    )
