"""Construcción de observaciones para el backtest walk-forward (Fase C, 2026-10-11).

Para cada símbolo y cada día elegible genera UNA entrada hipotética:

- Score: el cuantitativo determinista de producción
  (`calculate_quantitative_score`), alimentado con los MISMOS indicadores diarios
  (`calculate_indicators`) y la misma distancia %-vs-SMA que el pre-filtro. Motor
  compartido con producción (decisión del plan, sección 15.5): solo cambia la
  fuente de datos y el reloj.
- Decisión: a las 00:00 UTC del día D, con la última vela DIARIA cerrada (la del
  día D-1).
- Entrada: al cierre de la vela de 1h de las 23:00 del día D-1 (precio a las
  00:00 UTC).
- Desenlace: `simular_salida`, las reglas de salida del motor paper (stop, cierre
  parcial, trailing, comisión).

Sin look-ahead: los indicadores son causales (cada valor usa solo velas hasta la
suya; lo comprueba un test que recalcula con la serie truncada) y el desenlace
mira solo velas de 1h POSTERIORES a la entrada.

Funciones puras sobre `Decimal`: sin base de datos, red ni LLM.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.data.exchange_client import Candle
from app.data.exit_simulation import (
    MAXIMO_PRIMERO,
    SIN_FRICCION,
    Friccion,
    ResolvedorIntravela,
    retorno_marcado_a_mercado,
    simular_salida,
)
from app.data.indicator_calculator import (
    INDICATOR_NAME_DISTANCIA_MAX_90D,
    INDICATOR_NAME_RSI,
    INDICATOR_NAME_VOLUMEN_RELATIVO,
    SMA_PERIODS,
    calculate_indicators,
    sma_indicator_name,
)
from app.domain.rules.quantitative_score import calculate_quantitative_score
from app.domain.rules.walk_forward import ObservacionBacktest

_UN_DIA = timedelta(days=1)
_UNA_HORA = timedelta(hours=1)
_CIEN = Decimal(100)


def observaciones_de_simbolo(
    symbol: str,
    diarias: list[Candle],
    velas_1h: list[Candle],
    *,
    orden_intravela: str = MAXIMO_PRIMERO,
    resolvedor: ResolvedorIntravela | None = None,
    friccion: Friccion = SIN_FRICCION,
) -> list[ObservacionBacktest]:
    """Una observación por día con score calculable y entrada simulable. Ambas
    listas deben venir ordenadas por `open_time` y contener solo velas CERRADAS.

    `orden_intravela`: ver `app.data.exit_simulation`. El valor por defecto es el de
    todos los resultados publicados; otro valor solo se usa en el estudio de sensibilidad.
    `resolvedor`: ver `app.data.exit_simulation` (velas de 1 minuto en las velas ambiguas);
    por defecto `None` = resultados publicados.
    `friccion`: slippage y huecos de la prueba de estrés (`claude/protocolo-friccion.md`);
    por defecto `SIN_FRICCION` = resultados publicados."""
    if not diarias or not velas_1h:
        return []

    valores: dict[tuple[str, datetime], Decimal] = {
        (fila.indicator_name, fila.calculated_at): fila.value
        for fila in calculate_indicators(symbol, "1d", diarias)
    }
    indice_1h = {c["open_time"]: i for i, c in enumerate(velas_1h)}

    observaciones: list[ObservacionBacktest] = []
    for vela in diarias:
        abre = vela["open_time"]
        decision = abre + _UN_DIA  # 00:00 UTC del día siguiente: la vela diaria ya cerró
        sma_pct: list[Decimal | None] = []
        for periodo in SMA_PERIODS:
            media = valores.get((sma_indicator_name(periodo), abre))
            sma_pct.append(None if media is None or media == 0 else (vela["close"] - media) / media * _CIEN)
        score = calculate_quantitative_score(
            symbol,
            decision,
            sma_pct_values=sma_pct,
            volumen_relativo=valores.get((INDICATOR_NAME_VOLUMEN_RELATIVO, abre)),
            distancia_max_90d_pct=valores.get((INDICATOR_NAME_DISTANCIA_MAX_90D, abre)),
            rsi=valores.get((INDICATOR_NAME_RSI, abre)),
        )
        if score is None:
            continue
        i_entrada = indice_1h.get(decision - _UNA_HORA)
        if i_entrada is None or i_entrada + 1 >= len(velas_1h):
            continue
        salida = simular_salida(
            velas_1h, i_entrada, orden_intravela=orden_intravela, resolvedor=resolvedor, friccion=friccion
        )
        marcado = retorno_marcado_a_mercado(
            velas_1h, i_entrada, orden_intravela=orden_intravela, resolvedor=resolvedor, friccion=friccion
        )
        observaciones.append(
            ObservacionBacktest(
                symbol=symbol,
                dia=decision.astimezone(timezone.utc).date(),
                score=score.score_final,
                retorno_neto=None if salida is None else salida.retorno_neto,
                cierre_en=None if salida is None else decision + _UNA_HORA * salida.velas_hasta_cierre,
                factores=(
                    ("tendencia", score.score_tendencia),
                    ("volumen", score.score_volumen),
                    ("distancia_max_90d", score.score_distancia),
                    ("rsi", score.score_rsi),
                ),
                retorno_marcado=marcado,
                marcado_en=None if marcado is None else velas_1h[-1]["open_time"] + _UNA_HORA,
            )
        )
    return observaciones
