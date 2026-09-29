"""Verificación formal de indicadores ya guardados (sección 22/23 del plan:
formaliza lo que hasta ahora era una comparación manual contra TradingView).

Alcance deliberadamente limitado — dos cosas, ninguna más:

1. **Integridad de persistencia**: recalcula los indicadores de cada
   combinación símbolo/temporalidad a partir de las velas guardadas (usando
   la misma `calculate_indicators` ya validada por
   `tests/unit/test_indicator_calculator.py` contra valores calculados a
   mano) y compara ese resultado contra lo que realmente quedó en la tabla
   `indicadores`. Esto NO vuelve a validar la fórmula en sí — eso ya lo
   cubren los tests unitarios — sino que detecta bugs de otra capa: un
   `save_indicators` que perdió filas, un valor corrompido, un cálculo
   corrido con código viejo y nunca recalculado, etc.

2. **Invariantes matemáticos**: chequeos que deben cumplirse siempre por
   construcción, sin importar la fuente de datos (RSI entre 0 y 100, ATR y
   volumen relativo no negativos, distancia al máximo/ATH nunca positiva).

LO QUE ESTE MÓDULO NO HACE: no vuelve a comparar contra TradingView. Esa
validación (sección "Hecho cuando" de la Fase 1: "tus números coinciden con
TradingView") ya se hizo manualmente para BTCUSDT/1d (SMA-90/120/180, RSI) y
quedó aceptada por el usuario el 2026-09-19 — no hay una API pública estable
de TradingView contra la cual automatizarla, y anclar aquí un valor
"verificado" que en realidad no se recomprobó violaría la sección P3 del
plan (cero datos inventados). Si en el futuro se decide repetir esa
comparación con otro símbolo o temporalidad, sigue siendo manual.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from app.data.indicator_calculator import (
    INDICATOR_NAME_ATR,
    INDICATOR_NAME_DISTANCIA_ATH,
    INDICATOR_NAME_DISTANCIA_MAX_90D,
    INDICATOR_NAME_RSI,
    INDICATOR_NAME_VOLUMEN_RELATIVO,
    IndicatorRow,
    calculate_indicators,
)
from app.repositories.candles import get_candles
from app.repositories.indicators import get_indicators

# Tolerancia para comparar el valor recalculado (Decimal de precisión
# arbitraria) contra el valor persistido (Postgres NUMERIC(30, 12), que
# redondea al guardar). No se compara con `==`: el redondeo de Postgres al
# guardar puede diferir en el último dígito del redondeo que haría Python,
# y este chequeo busca corrupción/pérdida de datos, no discrepancias de
# redondeo en la doceava cifra decimal.
DEFAULT_TOLERANCE = Decimal("0.000001")

# Épsilon para los invariantes de "no positivo" (distancia al máximo/ATH):
# el máximo corrido incluye el high de la propia vela, así que el close de
# esa vela puede a lo sumo igualarlo, nunca superarlo — salvo por el mismo
# redondeo de NUMERIC(30, 12) al persistir.
_BOUNDS_EPSILON = Decimal("0.000001")


@dataclass(frozen=True)
class IndicatorMismatch:
    """Una discrepancia puntual entre lo recalculado y lo persistido."""

    calculated_at: object
    indicator_name: str
    kind: str  # "faltante" | "valor_distinto" | "inesperado"
    expected: Decimal | None
    actual: Decimal | None


@dataclass(frozen=True)
class BoundsViolation:
    """Un valor persistido que rompe un invariante matemático del indicador."""

    calculated_at: object
    indicator_name: str
    value: Decimal
    reason: str


@dataclass(frozen=True)
class VerificationResult:
    """Resultado de verificar una combinación símbolo/temporalidad."""

    symbol: str
    timeframe: str
    candles_used: int
    expected_count: int
    persisted_count: int
    mismatches: list[IndicatorMismatch] = field(default_factory=list)
    bounds_violations: list[BoundsViolation] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.mismatches and not self.bounds_violations


def _check_bounds(row: IndicatorRow) -> BoundsViolation | None:
    name = row.indicator_name
    value = row.value

    if name == INDICATOR_NAME_RSI:
        if value < 0 or value > 100:
            return BoundsViolation(row.calculated_at, name, value, "RSI fuera de [0, 100]")
    elif name == INDICATOR_NAME_ATR:
        if value < 0:
            return BoundsViolation(row.calculated_at, name, value, "ATR negativo")
    elif name == INDICATOR_NAME_VOLUMEN_RELATIVO:
        if value < 0:
            return BoundsViolation(row.calculated_at, name, value, "Volumen relativo negativo")
    elif name in (INDICATOR_NAME_DISTANCIA_ATH, INDICATOR_NAME_DISTANCIA_MAX_90D):
        if value > _BOUNDS_EPSILON:
            return BoundsViolation(
                row.calculated_at, name, value, "Distancia al máximo/ATH positiva (el cierre superó su propio máximo)"
            )
    return None


def verify_symbol_timeframe(
    session: Session, symbol: str, timeframe: str, tolerance: Decimal = DEFAULT_TOLERANCE
) -> VerificationResult:
    """Verifica una combinación símbolo/temporalidad.

    Si no hay velas guardadas, se declara el dato faltante (igual que
    `calculate_indicators_for_symbol_timeframe`) en vez de fallar: no correr
    el backfill para un símbolo todavía no es un error de este chequeo.
    """
    candles = get_candles(session, symbol, timeframe)
    if not candles:
        return VerificationResult(symbol, timeframe, candles_used=0, expected_count=0, persisted_count=0)

    expected_rows = calculate_indicators(symbol, timeframe, candles)
    persisted_rows = get_indicators(session, symbol, timeframe)

    expected_map: dict[tuple[object, str], Decimal] = {
        (row.calculated_at, row.indicator_name): row.value for row in expected_rows
    }
    persisted_map: dict[tuple[object, str], Decimal] = {
        (row.calculated_at, row.indicator_name): row.value for row in persisted_rows
    }

    mismatches: list[IndicatorMismatch] = []

    for key, expected_value in expected_map.items():
        calculated_at, indicator_name = key
        if key not in persisted_map:
            mismatches.append(
                IndicatorMismatch(calculated_at, indicator_name, "faltante", expected_value, None)
            )
            continue
        actual_value = persisted_map[key]
        if abs(actual_value - expected_value) > tolerance:
            mismatches.append(
                IndicatorMismatch(calculated_at, indicator_name, "valor_distinto", expected_value, actual_value)
            )

    for key in persisted_map.keys() - expected_map.keys():
        calculated_at, indicator_name = key
        mismatches.append(
            IndicatorMismatch(calculated_at, indicator_name, "inesperado", None, persisted_map[key])
        )

    bounds_violations = [
        violation for row in persisted_rows if (violation := _check_bounds(row)) is not None
    ]

    return VerificationResult(
        symbol=symbol,
        timeframe=timeframe,
        candles_used=len(candles),
        expected_count=len(expected_rows),
        persisted_count=len(persisted_rows),
        mismatches=mismatches,
        bounds_violations=bounds_violations,
    )


def verify_all(session: Session, symbols, timeframes) -> list[VerificationResult]:
    """Corre `verify_symbol_timeframe` para cada combinación símbolo x
    temporalidad. Solo lectura: a diferencia de `calculate_indicators_for_all`
    no hace `commit()` porque no escribe nada."""
    results: list[VerificationResult] = []
    for symbol in symbols:
        for timeframe in timeframes:
            results.append(verify_symbol_timeframe(session, symbol, timeframe))
    return results
