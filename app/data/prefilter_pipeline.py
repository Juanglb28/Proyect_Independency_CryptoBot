"""Orquesta el pre-filtro sobre el universo completo de símbolos (sección
4.2 del plan, Paso 6.1): lee velas e indicadores diarios ya calculados y
guardados (Paso 4) para los 20 símbolos de la whitelist, calcula el
puntaje de cada uno y selecciona los candidatos del día.

Mismo rol que `indicator_pipeline.py` cumple para los indicadores: separa
"calcular" (puro, sin base de datos — `prefilter.py`) de "leer de la base
de datos y calcular para todo el universo" (aquí).

`run_prefilter` NO persiste nada: solo devuelve los puntajes calculados, para
que un script o un test puedan inspeccionarlos. Quien guarda el resultado de
cada ciclo en `prefiltro_resultados` es el orquestador
(`app.orchestration.daily_cycle`), con `clasificar_resultados` de `prefilter.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.data.indicator_calculator import (
    INDICATOR_NAME_DISTANCIA_MAX_90D,
    INDICATOR_NAME_VOLUMEN_RELATIVO,
    IndicatorRow,
    SMA_PERIODS,
    sma_indicator_name,
)
from app.data.prefilter import PrefilterScore, calculate_prefilter_score, select_candidates
from app.domain.symbols import ALLOWED_SYMBOLS
from app.repositories.candles import get_candles
from app.repositories.indicators import get_indicators

_DAILY_TIMEFRAME = "1d"


@dataclass(frozen=True)
class PrefilterRunResult:
    """Resumen de correr el pre-filtro sobre todo el universo."""

    scores: tuple[PrefilterScore, ...]
    candidates: tuple[PrefilterScore, ...]
    symbols_sin_datos: tuple[str, ...]
    simbolos_excluidos: frozenset[str] = frozenset()
    """Símbolos que se quitaron de los candidatos por tener posición abierta o entrada
    pendiente (`excluir_simbolos_ocupados`). Vacío si no se aplicó ninguna exclusión."""


def _value_at(rows: list[IndicatorRow], indicator_name: str, calculated_at) -> Decimal | None:
    """Valor de `indicator_name` calculado exactamente en `calculated_at`.

    Se exige coincidencia EXACTA (no "el más reciente disponible") para no
    mezclar, en el mismo puntaje, un indicador calculado sobre una vela con
    el cierre de una vela distinta — aunque en la práctica, con el
    histórico de indicadores completo, el indicador más reciente y la
    última vela siempre deberían coincidir. Esto es lo mismo que exige la
    sección 15 del plan (trazabilidad / look-ahead bias): poder responder
    "qué sabía el sistema exactamente en este instante" sin ambigüedad.
    """
    for row in rows:
        if row.indicator_name == indicator_name and row.calculated_at == calculated_at:
            return row.value
    return None


def run_prefilter(session: Session) -> PrefilterRunResult:
    """Calcula el pre-filtro para los 20 símbolos de `ALLOWED_SYMBOLS`,
    usando siempre la última vela diaria guardada de cada uno (sección
    4.2: alcance decidido como "solo velas diarias").

    Un símbolo sin velas diarias, o al que le falte cualquiera de los tres
    factores (alguna SMA, volumen relativo o distancia a 90d) en la fecha
    de su última vela, se reporta en `symbols_sin_datos` en vez de
    forzarle un puntaje (sección P3 del plan) — el resto del universo se
    sigue evaluando con normalidad.
    """
    scores: list[PrefilterScore] = []
    symbols_sin_datos: list[str] = []

    for symbol in ALLOWED_SYMBOLS:
        candles = get_candles(session, symbol, _DAILY_TIMEFRAME)
        if not candles:
            symbols_sin_datos.append(symbol)
            continue

        indicators = get_indicators(session, symbol, _DAILY_TIMEFRAME)
        last_close = candles[-1]["close"]
        last_open_time = candles[-1]["open_time"]

        sma_pct_values: list[Decimal | None] = []
        for period in SMA_PERIODS:
            sma_value = _value_at(indicators, sma_indicator_name(period), last_open_time)
            if sma_value is None or sma_value == 0:
                sma_pct_values.append(None)
            else:
                sma_pct_values.append((last_close - sma_value) / sma_value * Decimal(100))

        volumen_relativo = _value_at(indicators, INDICATOR_NAME_VOLUMEN_RELATIVO, last_open_time)
        distancia_max_90d_pct = _value_at(
            indicators, INDICATOR_NAME_DISTANCIA_MAX_90D, last_open_time
        )

        score = calculate_prefilter_score(
            symbol,
            last_open_time,
            sma_pct_values=sma_pct_values,
            volumen_relativo=volumen_relativo,
            distancia_max_90d_pct=distancia_max_90d_pct,
        )
        if score is None:
            symbols_sin_datos.append(symbol)
        else:
            scores.append(score)

    candidates = select_candidates(scores)

    return PrefilterRunResult(
        scores=tuple(scores),
        candidates=tuple(candidates),
        symbols_sin_datos=tuple(symbols_sin_datos),
    )


def excluir_simbolos_ocupados(resultado: PrefilterRunResult, ocupados: frozenset[str]) -> PrefilterRunResult:
    """Quita de los candidatos los símbolos con posición abierta o entrada pendiente.

    Decisión del 2026-10-26: el sistema no aumenta posiciones (la validación de riesgo
    rechaza con `SIMBOLO_YA_ABIERTO` cualquier entrada sobre un símbolo ocupado), así
    que analizar con IA un símbolo ocupado es un gasto sin salida posible. La exclusión
    se hace ANTES del corte a los mejores `MAX_CANDIDATOS_PREFILTRO`: el cupo pasa al
    siguiente. Los puntajes NO cambian; solo cambian los candidatos y queda anotado
    qué símbolos se excluyeron (para registrar `DESCARTADO_POR_POSICION_ABIERTA`).

    Función pura: no consulta nada; quien llama entrega los símbolos ocupados. Si
    ninguno de ellos tiene puntaje, devuelve el resultado tal cual."""
    afectados = frozenset(s.symbol for s in resultado.scores) & ocupados
    if not afectados:
        return resultado
    return PrefilterRunResult(
        scores=resultado.scores,
        candidates=tuple(select_candidates(list(resultado.scores), excluir=afectados)),
        symbols_sin_datos=resultado.symbols_sin_datos,
        simbolos_excluidos=afectados,
    )
