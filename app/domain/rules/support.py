"""Definiciones de soporte para el estudio de la hipótesis H1 (2026-10-08).

SOLO se usan en el estudio offline (`app/data/support_study.py`); NO forman
parte del score vivo ni se entregan a ningún agente. Los valores de abajo
están fijados en el protocolo `claude/protocolo-h1-soporte.md` ANTES de ver
resultados: cambiar cualquiera crea una hipótesis nueva, no un ajuste.

Todo es puro y trabaja con `Decimal`; sin base de datos, reloj ni red.
"""

from __future__ import annotations

from decimal import Decimal

FRACTAL_N = 5
"""S1: un mínimo de oscilación es estrictamente menor que las N velas
anteriores y las N siguientes."""

VENTANA_FRACTAL_VELAS = 180
"""S1: solo cuentan mínimos de oscilación ocurridos en las últimas 180 velas
diarias."""

VENTANA_MINIMO_VELAS = 90
"""S2: mínimo de los low de las últimas 90 velas diarias cerradas."""

TOLERANCIA_CERCANIA = Decimal("0.02")
"""'Cerca' = el cierre está entre el soporte y soporte × 1,02."""

UMBRAL_RSI_4H = Decimal("35")
"""Sobreventa: RSI-14 de la última vela de 4h cerrada < 35."""

VELAS_DIARIAS_MINIMAS = 190
VELAS_4H_MINIMAS = 200
"""Calentamiento: sin esta historia previa el día no es elegible (para ninguna
definición ni para la línea base, así el universo es el mismo)."""


def indices_minimos_oscilacion(lows: list[Decimal], n: int = FRACTAL_N) -> list[int]:
    """Índices `t` donde `lows[t]` es estrictamente menor que `lows[t-n..t-1]`
    y que `lows[t+1..t+n]`. Un empate con cualquier vecina NO cuenta."""
    if n < 1:
        raise ValueError("n debe ser >= 1")
    indices: list[int] = []
    for t in range(n, len(lows) - n):
        nivel = lows[t]
        if all(nivel < lows[t - k] and nivel < lows[t + k] for k in range(1, n + 1)):
            indices.append(t)
    return indices


def soporte_fractal_desde_indices(
    lows: list[Decimal],
    indices_oscilacion: list[int],
    ultimo_indice: int,
    precio: Decimal,
    *,
    n: int = FRACTAL_N,
    ventana: int = VENTANA_FRACTAL_VELAS,
) -> Decimal | None:
    """S1 usando los mínimos de oscilación ya calculados sobre la serie
    completa. Es equivalente a recalcular sobre `lows[: ultimo_indice + 1]`
    (un mínimo en `t` solo depende de `t ± n`) siempre que se exija `t + n <=
    ultimo_indice`, o sea, que ya haya `n` velas posteriores CERRADAS: eso evita
    mirar el futuro y lo comprueba `tests/unit/test_support_rules.py`."""
    primera = ultimo_indice + 1 - ventana
    candidatos = [
        lows[t] for t in indices_oscilacion if t >= primera and t + n <= ultimo_indice and lows[t] <= precio
    ]
    return max(candidatos) if candidatos else None


def soporte_fractal(
    lows: list[Decimal], precio: Decimal, *, n: int = FRACTAL_N, ventana: int = VENTANA_FRACTAL_VELAS
) -> Decimal | None:
    """S1. `lows` son los low de las velas diarias CERRADAS hasta el día
    anterior a la decisión (la última es la más reciente). Devuelve el mínimo
    de oscilación confirmado, ocurrido en las últimas `ventana` velas, de mayor
    nivel que esté por debajo o igual a `precio`; `None` si no hay."""
    return soporte_fractal_desde_indices(
        lows, indices_minimos_oscilacion(lows, n), len(lows) - 1, precio, n=n, ventana=ventana
    )


def soporte_minimo_90d(lows: list[Decimal], *, ventana: int = VENTANA_MINIMO_VELAS) -> Decimal | None:
    """S2. Mínimo de las últimas `ventana` velas; `None` si hay menos."""
    if len(lows) < ventana:
        return None
    return min(lows[-ventana:])


def esta_cerca(precio: Decimal, nivel: Decimal | None, tolerancia: Decimal = TOLERANCIA_CERCANIA) -> bool:
    """`nivel <= precio <= nivel × (1 + tolerancia)`. Sin soporte -> False."""
    if nivel is None:
        return False
    return nivel <= precio <= nivel * (Decimal(1) + tolerancia)
