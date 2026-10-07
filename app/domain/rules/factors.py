"""Análisis por factor (sección 8.8 del plan, `factor_analysis`).

Función pura y determinista: recibe operaciones cerradas y los factores con
los que se decidieron (guardados en `factores` ANTES de entrar) y devuelve,
por cada uno de los 4 factores ponderados, lo que pide el plan: peso asignado,
frecuencia, resultado promedio, win rate, contribución observada y confianza.
No usa IA ni lee la base de datos (P1).

## Decisiones del usuario (2026-10-07)

- **Contribución observada** = mismo método que la validación del score
  (sección 8.7), aplicado a cada factor: se parten las operaciones en terciles
  del PUNTAJE DEL FACTOR; contribución = expectancy del tercio alto menos la
  del tercio bajo, con intervalo bootstrap 95% (1000 iteraciones, semilla
  fija 20261005). Se logra reutilizando `validate_score_discrimination` con el
  puntaje del factor en lugar del score global: un solo método, sin segunda
  implementación que pueda divergir.
- **Frecuencia** = operaciones cerradas en las que el factor fue puntuado
  (cantidad y % del total de operaciones).
- **Confianza** = cuántas veces el Consolidador marcó el factor como
  alta/media/baja (la etiqueta guardada, nunca recalculada), más el estado
  estadístico por tamaño de muestra (`EstadoValidacion`: menos de 3 = muestra
  insuficiente, 3 a 29 = preliminar, 30 o más = evaluable).

## Qué NO hace

- No califica ni interpreta: muestra los números junto al peso asignado y la
  lectura (¿los factores de más peso se relacionan con el resultado?) es del
  usuario, igual que en los terciles del score.
- No corrige por comparaciones múltiples: son 4 factores con IC 95% cada uno,
  así que alguno puede salir "discrimina" por azar; la interfaz lo advierte.
- Un factor puntuado con menos de 3 operaciones no tiene terciles: se dice
  explícitamente, no se rellena.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from app.domain.rules.results import (
    BOOTSTRAP_ITERACIONES,
    BOOTSTRAP_SEMILLA,
    NIVEL_DE_CONFIANZA_PCT,
    MUESTRA_MINIMA_PARA_TERCILES,
    EstadoValidacion,
    Terciles,
    TradeResult,
    ValidationReport,
    expectancy_usd,
    split_terciles,
    validate_score_discrimination,
    win_rate_pct,
)

FACTORES: tuple[str, ...] = (
    "tendencia_momentum",
    "volumen_liquidez",
    "posicion_rangos_ath",
    "contexto_eventos",
)
"""Los 4 factores ponderados que puntúa el Consolidador (ver `scoring.py`)."""

_CIEN = Decimal(100)
_ESCALA_PCT = Decimal("1E-2")


@dataclass(frozen=True)
class FactorDeOperacion:
    """Un factor tal como se guardó para la propuesta de una operación."""

    position_id: uuid.UUID
    factor: str
    peso: Decimal
    puntaje: Decimal
    confianza: str | None
    """`alta` / `media` / `baja`, o `None` si no se guardó."""


@dataclass(frozen=True)
class ConteoDeConfianza:
    alta: int
    media: int
    baja: int
    sin_dato: int


@dataclass(frozen=True)
class GrupoDeFactor:
    """Operaciones de un extremo (tercio alto o bajo) del puntaje del factor."""

    n: int
    expectancy_usd: Decimal | None
    win_rate_pct: Decimal | None


@dataclass(frozen=True)
class AnalisisDeFactor:
    factor: str
    peso: Decimal | None
    """Peso asignado guardado. `None` si hubo operaciones con pesos distintos
    (se explica en `notas`) o si el factor no se puntuó en ninguna."""
    n_operaciones: int
    pct_de_operaciones: Decimal | None
    """`n_operaciones` sobre el total de operaciones cerradas (`None` sin operaciones)."""
    resultado_promedio_usd: Decimal | None
    """P&L neto medio de las operaciones en las que el factor fue puntuado."""
    win_rate_pct: Decimal | None
    confianza: ConteoDeConfianza
    estado: EstadoValidacion
    tercil_alto: GrupoDeFactor | None
    tercil_bajo: GrupoDeFactor | None
    contribucion_usd: Decimal | None
    """Expectancy del tercio alto menos la del bajo del puntaje del factor."""
    ci_lower: Decimal | None
    ci_upper: Decimal | None
    discrimina: bool | None
    """`True` si el límite inferior del IC 95% es > 0. Solo orientativo si `estado` es PRELIMINAR."""
    notas: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class FactorAnalysisReport:
    n_operaciones: int
    n_con_propuesta_y_factores: int
    factores: tuple[AnalisisDeFactor, ...]
    iterations: int
    seed: int
    confidence_level_pct: int
    notas: tuple[str, ...] = field(default=())


def _hay_empates_en_el_limite(terciles: Terciles) -> bool:
    """¿Algún puntaje del tercio alto (o bajo) se repite fuera de él, en el límite?"""
    alto = [t.initial_opportunity_score for t in terciles.superior]
    bajo = [t.initial_opportunity_score for t in terciles.inferior]
    medio = [t.initial_opportunity_score for t in terciles.medio]
    if not alto or not bajo:
        return False
    return min(alto) == max(medio + bajo) or max(bajo) == min(medio + alto)


def _grupo(trades: Sequence[TradeResult]) -> GrupoDeFactor:
    return GrupoDeFactor(len(trades), expectancy_usd(trades), win_rate_pct(trades))


def _analizar_un_factor(
    factor: str,
    trades: Sequence[TradeResult],
    factores_por_posicion: dict[uuid.UUID, FactorDeOperacion],
    *,
    iterations: int,
    seed: int,
) -> AnalisisDeFactor:
    # Cada operación se evalúa con el puntaje de ESTE factor en lugar del score
    # global: así se reutiliza, sin copiarlo, el método de la sección 8.7.
    con_factor = [
        dataclasses.replace(t, initial_opportunity_score=factores_por_posicion[t.position_id].puntaje)
        for t in trades
        if t.position_id in factores_por_posicion
    ]
    n = len(con_factor)
    notas: list[str] = []

    pesos = {factores_por_posicion[t.position_id].peso for t in con_factor}
    peso = next(iter(pesos)) if len(pesos) == 1 else None
    if len(pesos) > 1:
        notas.append("El peso asignado cambió entre operaciones: " + ", ".join(str(p) for p in sorted(pesos)) + ".")

    etiquetas = [factores_por_posicion[t.position_id].confianza for t in con_factor]
    confianza = ConteoDeConfianza(
        alta=etiquetas.count("alta"),
        media=etiquetas.count("media"),
        baja=etiquetas.count("baja"),
        sin_dato=sum(1 for e in etiquetas if e not in ("alta", "media", "baja")),
    )

    informe: ValidationReport = validate_score_discrimination(con_factor, iterations=iterations, seed=seed)
    terciles = split_terciles(con_factor)

    # Un factor que puntuó EXACTAMENTE igual en todas las operaciones no tiene
    # nada que comparar: el desempate por fecha de entrada (regla del score)
    # repartiría las operaciones en "alto" y "bajo" por antigüedad y mostraría
    # una contribución que no viene del factor. Se calcula nada en vez de eso (P3).
    sin_variacion = n >= MUESTRA_MINIMA_PARA_TERCILES and len({t.initial_opportunity_score for t in con_factor}) == 1
    if sin_variacion:
        notas.append(
            "El factor tuvo el mismo puntaje en todas las operaciones: no hay variación que comparar, "
            "así que no se calcula la contribución."
        )
        terciles = None
    elif terciles is not None and _hay_empates_en_el_limite(terciles):
        notas.append(
            "Hay puntajes repetidos en el límite entre terciles: el desempate se hace por fecha de entrada "
            "(la misma regla que el score), así que la separación alto/bajo es parcialmente arbitraria."
        )

    if terciles is None and not sin_variacion:
        notas.append("Menos de 3 operaciones con este factor: no se pueden formar el tercio alto y el bajo.")
    elif informe.status == EstadoValidacion.PRELIMINAR:
        notas.append(
            f"Preliminar: {n} operaciones con este factor, por debajo de las {informe.min_trades_plan} que pide el plan."
        )
    if terciles is not None and n % 3:
        notas.append(f"{n % 3} operación(es) de rango medio quedaron fuera de ambos extremos (n no es múltiplo de 3).")

    return AnalisisDeFactor(
        factor=factor,
        peso=peso,
        n_operaciones=n,
        pct_de_operaciones=(
            None if not trades else (Decimal(n) / len(trades) * _CIEN).quantize(_ESCALA_PCT)
        ),
        resultado_promedio_usd=expectancy_usd(con_factor),
        win_rate_pct=win_rate_pct(con_factor),
        confianza=confianza,
        estado=informe.status,
        tercil_alto=None if terciles is None else _grupo(terciles.superior),
        tercil_bajo=None if terciles is None else _grupo(terciles.inferior),
        contribucion_usd=None if sin_variacion else informe.score_discrimination,
        ci_lower=None if sin_variacion else informe.ci_lower,
        ci_upper=None if sin_variacion else informe.ci_upper,
        discrimina=None if sin_variacion else informe.discriminates,
        notas=tuple(notas),
    )


def analyze_factors(
    trades: Sequence[TradeResult],
    factores: Sequence[FactorDeOperacion],
    *,
    iterations: int = BOOTSTRAP_ITERACIONES,
    seed: int = BOOTSTRAP_SEMILLA,
) -> FactorAnalysisReport:
    """Análisis por factor de las operaciones cerradas `trades`.

    `factores` son los factores guardados de cada operación (uno por factor y
    operación). Las operaciones sin propuesta o sin factores cuentan en el
    total, pero no participan en ningún factor. Siempre devuelve los 4 factores
    en el orden de `FACTORES`, incluso sin datos.
    """
    por_factor: dict[str, dict[uuid.UUID, FactorDeOperacion]] = {f: {} for f in FACTORES}
    desconocidos: set[str] = set()
    for fo in factores:
        if fo.factor not in por_factor:
            desconocidos.add(fo.factor)
            continue
        if fo.position_id in por_factor[fo.factor]:
            raise ValueError(f"factor {fo.factor!r} repetido para la posición {fo.position_id}")
        por_factor[fo.factor][fo.position_id] = fo

    notas: list[str] = []
    if desconocidos:
        notas.append("Se ignoraron factores no reconocidos: " + ", ".join(sorted(desconocidos)) + ".")

    con_factores = {fo.position_id for fo in factores if fo.factor in por_factor}
    n_con = sum(1 for t in trades if t.position_id in con_factores)
    if n_con < len(trades):
        notas.append(
            f"{len(trades) - n_con} operación(es) sin propuesta o sin factores guardados no participan en el análisis."
        )

    return FactorAnalysisReport(
        n_operaciones=len(trades),
        n_con_propuesta_y_factores=n_con,
        factores=tuple(
            _analizar_un_factor(f, trades, por_factor[f], iterations=iterations, seed=seed) for f in FACTORES
        ),
        iterations=iterations,
        seed=seed,
        confidence_level_pct=NIVEL_DE_CONFIANZA_PCT,
        notas=tuple(notas),
    )
