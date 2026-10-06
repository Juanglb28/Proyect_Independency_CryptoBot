"""Validación de estrategia: ¿el score predice algo? (secciones 8.7/8.9).

Reúne, para las seis preguntas de la sección 8.7, lo que SÍ puede responderse
hoy con datos reales y deja explícitamente PENDIENTE lo que no (no se rellena
con aproximaciones). El veredicto de la pregunta 1 es el intervalo bootstrap
fijado por el plan; aquí solo se muestra.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy.orm import Session

from app.domain.rules.results import PerformanceMetrics, ValidationReport
from app.results.performance_service import compute_performance
from app.results.validation_service import validate_score
from app.services._periodo import inicio_del_historial


class EstadoPregunta(str, Enum):
    DISPONIBLE = "DISPONIBLE"
    PENDIENTE = "PENDIENTE"


@dataclass(frozen=True)
class PreguntaDeValidacion:
    numero: int
    texto: str
    estado: EstadoPregunta
    nota: str


_PREGUNTAS: tuple[PreguntaDeValidacion, ...] = (
    PreguntaDeValidacion(
        1, "¿El tercio superior del score obtiene mejor expectancy que el tercio inferior?", EstadoPregunta.DISPONIBLE,
        "Intervalo de confianza 95% por bootstrap; discrimina si el límite inferior es mayor que cero.",
    ),
    PreguntaDeValidacion(
        2, "¿El win rate aumenta de forma razonablemente monotónica por buckets?", EstadoPregunta.DISPONIBLE,
        "Se muestran win rate y expectancy de cada tercio; la lectura de la tendencia es tuya, el sistema no la califica.",
    ),
    PreguntaDeValidacion(
        3, "¿Los factores con mayor peso muestran relación observable con el resultado?", EstadoPregunta.PENDIENTE,
        "Requiere un análisis por factor que todavía no existe (factor_analysis, sección 8.8). Con menos de 30 "
        "operaciones tampoco habría muestra suficiente.",
    ),
    PreguntaDeValidacion(
        4, "¿La relación se mantiene fuera de la muestra usada para ajustar parámetros?", EstadoPregunta.PENDIENTE,
        "Requiere el backtest de la Fase 5. Hoy no se ha ajustado ningún parámetro con resultados.",
    ),
    PreguntaDeValidacion(
        5, "¿La ventaja desaparece al incluir comisiones y slippage?", EstadoPregunta.DISPONIBLE,
        "Se compara el P&L bruto con el neto. El slippage no se estima en la v1 (se trata como 0).",
    ),
    PreguntaDeValidacion(
        6, "¿El sistema supera al benchmark bajo una comparación temporal equivalente?", EstadoPregunta.DISPONIBLE,
        "Retorno del sistema frente a BTC comprado y mantenido durante el mismo periodo.",
    ),
)


@dataclass(frozen=True)
class ValidationView:
    generado_en: datetime
    periodo_inicio: datetime
    informe: ValidationReport
    terciles: tuple[PerformanceMetrics, ...]
    global_: PerformanceMetrics
    preguntas: tuple[PreguntaDeValidacion, ...]


def get_validation_view(session: Session, *, initial_capital: Decimal, now: datetime) -> ValidationView:
    inicio = inicio_del_historial(session, now=now)
    metricas = compute_performance(session, period_start=inicio, period_end=now, initial_capital=initial_capital)
    return ValidationView(
        generado_en=now,
        periodo_inicio=inicio,
        informe=validate_score(session),
        terciles=tuple(m for m in metricas if m.scope_type == "SCORE_TERCIL"),
        global_=metricas[0],
        preguntas=_PREGUNTAS,
    )
