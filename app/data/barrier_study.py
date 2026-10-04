"""Estudio descriptivo de barreras: "¿qué toca primero, el stop o el objetivo?"
(Paso 8, 2026-10-04).

Motivo: el usuario propuso cambiar stop -8% / objetivo +12% (sección 9 del
plan) por -6% / +8%. Antes de tocar una regla que afecta dinero, este módulo
mide con las velas históricas YA guardadas con qué frecuencia el precio toca
cada barrera primero, partiendo de una entrada cualquiera. Es un insumo para
decidir, NO un backtest ni una regla.

Qué es y qué NO es:

- Es **incondicional**: no usa el score, ni indicadores, ni eventos. Entra
  "a ciegas" a intervalos regulares. Responde cuánto ruido normal de precio
  tiene cada moneda frente a un stop dado, no si una propuesta del sistema
  funciona (eso lo mide la validación del Paso 9 con operaciones paper).
- No ajusta ningún parámetro con estos resultados de forma automática: la
  decisión sobre las reglas sigue siendo del usuario (master prompt,
  sección 30).
- No simula comisiones, el cierre parcial del 50% ni el trailing: mide solo
  el primer toque de cada barrera.
- Las entradas de distintas monedas no son independientes (el mercado cripto
  se mueve en bloque) y las entradas diarias de una misma moneda se solapan
  en el tiempo. Los porcentajes agregados son orientativos, no una prueba
  estadística.

Sin look-ahead: la entrada es el cierre de la vela de partida y solo se
miran velas POSTERIORES. Convención de igualdad idéntica a
`app/domain/rules/risk.py`: el stop se alcanza con `low <= nivel` y el
objetivo con `high >= nivel`. Si una misma vela toca ambos niveles no se
puede saber cuál fue primero con datos OHLC: se cuenta aparte como
`AMBIGUA`, sin repartirla a favor de ninguno.

Solo funciones puras sobre `Decimal`: sin base de datos, Binance ni LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from statistics import median

from app.data.exchange_client import Candle
from app.domain.rules.risk import COMISION_POR_LADO

_HORAS_POR_TIMEFRAME = {"1h": 1, "4h": 4, "12h": 12, "1d": 24}


class ResultadoBarrera(str, Enum):
    TARGET_PRIMERO = "TARGET_PRIMERO"
    STOP_PRIMERO = "STOP_PRIMERO"
    AMBIGUA = "AMBIGUA"
    SIN_RESOLVER = "SIN_RESOLVER"
    """Se acabaron los datos antes de tocar cualquiera de las dos barreras
    (observación censurada): no se cuenta ni a favor ni en contra."""


@dataclass(frozen=True)
class ParBarreras:
    stop_pct: Decimal
    target_pct: Decimal

    def __post_init__(self) -> None:
        if not (Decimal(0) < self.stop_pct < Decimal(1)) or self.target_pct <= 0:
            raise ValueError(
                f"Porcentajes inválidos: stop={self.stop_pct} (debe estar en (0,1)), "
                f"objetivo={self.target_pct} (debe ser > 0). Usa fracciones: 0.06 = 6%."
            )


@dataclass(frozen=True)
class EvaluacionEntrada:
    resultado: ResultadoBarrera
    velas_hasta_resolver: int | None
    """Cuántas velas después de la de entrada se tocó la barrera (1 = la
    vela siguiente). None si `SIN_RESOLVER`."""


@dataclass(frozen=True)
class ResumenBarreras:
    par: ParBarreras
    entradas: int
    target_primero: int
    stop_primero: int
    ambiguas: int
    sin_resolver: int
    mediana_velas_hasta_resolver: float | None
    """Mediana sobre las entradas resueltas (target o stop), en velas."""

    @property
    def resueltas(self) -> int:
        return self.target_primero + self.stop_primero

    @property
    def tasa_target(self) -> Decimal | None:
        """Fracción de entradas resueltas que tocaron el objetivo primero.
        None si no hubo ninguna resuelta."""
        if self.resueltas == 0:
            return None
        return Decimal(self.target_primero) / Decimal(self.resueltas)


def evaluate_entry(candles: list[Candle], start_index: int, par: ParBarreras) -> EvaluacionEntrada:
    """Entra al cierre de `candles[start_index]` y recorre las velas
    siguientes hasta el primer toque de una barrera."""
    entrada = candles[start_index]["close"]
    nivel_stop = entrada * (Decimal(1) - par.stop_pct)
    nivel_target = entrada * (Decimal(1) + par.target_pct)

    for offset, vela in enumerate(candles[start_index + 1 :], start=1):
        toca_stop = vela["low"] <= nivel_stop
        toca_target = vela["high"] >= nivel_target
        if toca_stop and toca_target:
            return EvaluacionEntrada(ResultadoBarrera.AMBIGUA, offset)
        if toca_stop:
            return EvaluacionEntrada(ResultadoBarrera.STOP_PRIMERO, offset)
        if toca_target:
            return EvaluacionEntrada(ResultadoBarrera.TARGET_PRIMERO, offset)
    return EvaluacionEntrada(ResultadoBarrera.SIN_RESOLVER, None)


def evaluate_symbol(
    candles: list[Candle], pares: list[ParBarreras], *, paso_velas: int
) -> dict[ParBarreras, list[EvaluacionEntrada]]:
    """Evalúa una entrada cada `paso_velas` velas (la primera en el índice 0)
    para cada par de barreras. Las velas deben venir en orden ascendente por
    `open_time` (así las entrega `repositories.candles.get_candles`)."""
    if paso_velas < 1:
        raise ValueError("paso_velas debe ser >= 1")
    evaluaciones: dict[ParBarreras, list[EvaluacionEntrada]] = {par: [] for par in pares}
    # La última vela no puede ser de entrada: no hay velas posteriores.
    for indice in range(0, len(candles) - 1, paso_velas):
        for par in pares:
            evaluaciones[par].append(evaluate_entry(candles, indice, par))
    return evaluaciones


def summarize(par: ParBarreras, evaluaciones: list[EvaluacionEntrada]) -> ResumenBarreras:
    def cuenta(r: ResultadoBarrera) -> int:
        return sum(1 for e in evaluaciones if e.resultado == r)

    resueltas = [
        e.velas_hasta_resolver
        for e in evaluaciones
        if e.resultado in (ResultadoBarrera.TARGET_PRIMERO, ResultadoBarrera.STOP_PRIMERO)
        and e.velas_hasta_resolver is not None
    ]
    return ResumenBarreras(
        par=par,
        entradas=len(evaluaciones),
        target_primero=cuenta(ResultadoBarrera.TARGET_PRIMERO),
        stop_primero=cuenta(ResultadoBarrera.STOP_PRIMERO),
        ambiguas=cuenta(ResultadoBarrera.AMBIGUA),
        sin_resolver=cuenta(ResultadoBarrera.SIN_RESOLVER),
        mediana_velas_hasta_resolver=float(median(resueltas)) if resueltas else None,
    )


def break_even_target_rate(par: ParBarreras, *, comision_por_lado: Decimal = COMISION_POR_LADO) -> Decimal:
    """Fracción de operaciones que deben tocar el objetivo primero para que
    la ganancia esperada sea 0, con comisión de ida y vuelta, en un modelo
    SIMPLIFICADO de dos resultados (sin cierre parcial ni trailing):

        p × (objetivo − 2c) = (1 − p) × (stop + 2c)

    es decir `p = (stop + 2c) / (objetivo + stop)`. Sirve solo para comparar
    contra `ResumenBarreras.tasa_target`."""
    costo = 2 * comision_por_lado
    return (par.stop_pct + costo) / (par.target_pct + par.stop_pct)


def velas_a_dias(velas: float, timeframe: str) -> float:
    if timeframe not in _HORAS_POR_TIMEFRAME:
        raise ValueError(f"Temporalidad no soportada: {timeframe!r}")
    return velas * _HORAS_POR_TIMEFRAME[timeframe] / 24
