"""Servicio de `performance_metrics` (Paso 9; sección 8.6 del plan).

Reconstruye las métricas agregadas de un periodo a partir de los hechos
(`trade_results` y `equity_observaciones`), llamando a las funciones puras de
`app.domain.rules.results`. Es DERIVADO: recalcular el mismo periodo
reemplaza las filas anteriores y, si se borrara la tabla, este servicio la
reconstruye entera (mismos datos, mismas cifras; `calculated_at` es el único
campo que cambia, y es el `now` que pasa quien llama).

Qué se calcula por periodo `[period_start, period_end]` (las operaciones
cuentan por su hora de SALIDA):

- `GLOBAL`: todas las operaciones, con la curva de equity del periodo
  (drawdown máximo) y el benchmark de BTC del periodo.
- `SYMBOL`: una fila por activo con operaciones.
- `SCORE_TERCIL`: `SUPERIOR` / `MEDIO` / `INFERIOR` si hay al menos 3
  operaciones con score.

SERIES (decisión del 2026-10-12): las operaciones de OBSERVACIÓN (candidatos del
pre-filtro abiertos sin IA) se miden aparte de las de la ESTRATEGIA, porque su
puntaje es de otra escala y no pasan por el riesgo del capital. Todas las
funciones reciben `serie`: `ESTRATEGIA` (por defecto, lo que valida el plan),
`OBSERVACION`, o `None` para la comparación conjunta. La tabla
`performance_metrics` guarda SOLO la serie de estrategia (no tiene columna de
serie); las demás se calculan al pedirlas, que es válido porque son derivadas.
La curva de equity es la del capital de la estrategia: sin ella (observación o
conjunto) el drawdown máximo queda `NULL`, no se inventa.

El drawdown máximo y el benchmark describen la trayectoria del CAPITAL y de
BTC, no la de un activo o un tercil: solo se calculan en `GLOBAL`; en los
demás alcances quedan `NULL` (no se inventa una curva por activo).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.enums import AlcanceMetricas, SerieOperacion
from app.domain.rules.results import (
    PerformanceMetrics,
    PuntoDeSerie,
    TradeResult,
    calculate_performance,
    split_terciles,
)
from app.repositories.equity import list_observaciones_between
from app.repositories.performance import upsert_performance
from app.repositories.trade_results import list_trade_results
from app.results.benchmark_service import calculate_period_benchmark
from app.results.trade_result_service import to_domain

_NOMBRES_TERCIL = ("SUPERIOR", "MEDIO", "INFERIOR")


def load_trades(
    session: Session,
    *,
    period_start: datetime | None,
    period_end: datetime | None,
    serie: SerieOperacion | None = SerieOperacion.ESTRATEGIA,
) -> list[TradeResult]:
    """Resultados cuya salida cae en el periodo (sin límites = todos), de la
    `serie` pedida (`None` = ambas)."""
    return [
        to_domain(r) for r in list_trade_results(session, exit_desde=period_start, exit_hasta=period_end, serie=serie)
    ]


def _exigir_utc(*momentos: datetime) -> None:
    for m in momentos:
        if m.tzinfo is None or m.utcoffset() != timedelta(0):
            raise ValueError("Las fechas deben tener zona horaria UTC explícita (sección 15.3 del plan).")


def history_start(
    session: Session, *, now: datetime, serie: SerieOperacion | None = SerieOperacion.ESTRATEGIA
) -> datetime:
    """Inicio del historial completo de la `serie`: la entrada más antigua (o
    `now` si aún no hay operaciones). Lo usan el comando `performance` y la
    interfaz."""
    return min((r.entry_time for r in list_trade_results(session, serie=serie)), default=now)


def compute_performance(
    session: Session,
    *,
    period_start: datetime,
    period_end: datetime,
    initial_capital: Decimal,
    serie: SerieOperacion | None = SerieOperacion.ESTRATEGIA,
) -> list[PerformanceMetrics]:
    """Calcula las métricas del periodo SIN guardar nada (solo lectura; es lo
    que usa la interfaz). Devuelve GLOBAL, SYMBOL (por símbolo) y
    SCORE_TERCIL, en ese orden. `recalculate_performance` es esto más guardar."""
    _exigir_utc(period_start, period_end)
    trades = load_trades(session, period_start=period_start, period_end=period_end, serie=serie)
    # La curva de equity es la del capital de la estrategia (ver el docstring del módulo).
    curva = (
        [
            PuntoDeSerie(o.observed_at, o.equity_usd)
            for o in list_observaciones_between(session, desde=period_start, hasta=period_end)
        ]
        if serie == SerieOperacion.ESTRATEGIA
        else []
    )
    benchmark = calculate_period_benchmark(session, period_start=period_start, period_end=period_end)

    def _calcular(scope: AlcanceMetricas, key: str | None, subset: list[TradeResult], *, global_: bool):
        return calculate_performance(
            subset,
            curva if global_ else [],
            benchmark if global_ else None,
            scope_type=scope.value,
            scope_key=key,
            period_start=period_start,
            period_end=period_end,
            initial_capital=initial_capital,
        )

    resultados = [_calcular(AlcanceMetricas.GLOBAL, None, trades, global_=True)]
    for symbol in sorted({t.symbol for t in trades}):
        resultados.append(
            _calcular(AlcanceMetricas.SYMBOL, symbol, [t for t in trades if t.symbol == symbol], global_=False)
        )
    terciles = split_terciles(trades)
    if terciles is not None:
        for nombre, grupo in zip(_NOMBRES_TERCIL, (terciles.superior, terciles.medio, terciles.inferior)):
            resultados.append(_calcular(AlcanceMetricas.SCORE_TERCIL, nombre, list(grupo), global_=False))
    return resultados


def recalculate_performance(
    session: Session,
    *,
    period_start: datetime,
    period_end: datetime,
    initial_capital: Decimal,
    now: datetime,
) -> list[PerformanceMetrics]:
    """Calcula y guarda las métricas del periodo de la serie de ESTRATEGIA (ver el
    docstring del módulo: la tabla no distingue series). Devuelve lo que guardó, en el orden GLOBAL, SYMBOL (por
    símbolo), SCORE_TERCIL."""
    _exigir_utc(now)
    resultados = compute_performance(
        session,
        period_start=period_start,
        period_end=period_end,
        initial_capital=initial_capital,
        serie=SerieOperacion.ESTRATEGIA,
    )
    for m in resultados:
        valores = dataclasses.asdict(m)
        valores["scope_type"] = AlcanceMetricas(m.scope_type)
        valores["calculated_at"] = now
        upsert_performance(session, valores)
    return resultados
