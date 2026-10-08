"""Reparación del historial de velas congeladas a medias (decisión del
usuario, 2026-10-08).

Antes de la corrección, la ingesta guardaba la vela EN CURSO y la dejaba
congelada para siempre con los valores del instante de la descarga. La migración
`e6a1c4b7d902` las marca `cerrada = false` (criterio exacto: se insertaron antes
de que la vela cerrara). Aquí se vuelven a descargar de Binance, se corrigen y
se recalculan los indicadores derivados desde las velas cerradas.

Esta capa NO hace commit ni rollback: quien llama decide. Así la misma ruta de
código sirve para la SIMULACIÓN (se ejecuta todo y se hace rollback, de modo que
lo que se ve es exactamente lo que pasaría) y para la ejecución real.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime

from sqlalchemy.orm import Session

from app.data.candle_ingestion import FetchFn, backfill_symbol_timeframe, velas_vencidas_sin_cerrar
from app.data.exchange_client import fetch_candles
from app.data.indicator_calculator import INDICATOR_NAME_RSI
from app.data.indicator_pipeline import calculate_indicators_for_symbol_timeframe
from app.repositories.candles import get_candles
from app.repositories.indicators import get_latest_indicator_value


@dataclass(frozen=True)
class ReparacionVela:
    open_time: datetime
    close_antes: Decimal
    close_despues: Decimal
    volumen_antes: Decimal
    volumen_despues: Decimal

    @property
    def cambio_close_pct(self) -> Decimal | None:
        if self.close_antes == 0:
            return None
        return (self.close_despues - self.close_antes) / self.close_antes * Decimal(100)


@dataclass(frozen=True)
class ReparacionSerie:
    symbol: str
    timeframe: str
    velas: tuple[ReparacionVela, ...]
    # Velas vencidas que siguen sin cerrar tras la reparación (Binance no las devolvió).
    restantes: int
    rsi_antes: Decimal | None
    rsi_despues: Decimal | None


def _valores(session: Session, symbol: str, timeframe: str, tiempos: set[datetime]) -> dict[datetime, tuple[Decimal, Decimal]]:
    return {
        c["open_time"]: (Decimal(c["close"]), Decimal(c["volume"]))
        for c in get_candles(session, symbol, timeframe, solo_cerradas=False)
        if c["open_time"] in tiempos
    }


def _ultimo_rsi(session: Session, symbol: str, timeframe: str, *, ahora: datetime) -> Decimal | None:
    valor = get_latest_indicator_value(
        session, symbol=symbol, timeframe=timeframe, indicator_name=INDICATOR_NAME_RSI, calculated_at_or_before=ahora
    )
    return None if valor is None else Decimal(valor)


def reparar_serie(
    session: Session,
    symbol: str,
    timeframe: str,
    *,
    ahora: datetime,
    fetch_fn: FetchFn = fetch_candles,
    exchange: object | None = None,
) -> ReparacionSerie | None:
    """Repara las velas vencidas sin cerrar de `symbol`/`timeframe`; `None` si
    no hay ninguna. Descarga desde la más antigua, actualiza (la ingesta ya
    descarta los indicadores derivados) y recalcula los indicadores."""
    vencidas = [
        open_time
        for s, tf, open_time in velas_vencidas_sin_cerrar(session, ahora=ahora, symbols=[symbol])
        if tf == timeframe
    ]
    if not vencidas:
        return None

    tiempos = set(vencidas)
    antes = _valores(session, symbol, timeframe, tiempos)
    rsi_antes = _ultimo_rsi(session, symbol, timeframe, ahora=ahora)

    backfill_symbol_timeframe(
        session, symbol, timeframe, since=min(vencidas), fetch_fn=fetch_fn, exchange=exchange, now=ahora
    )
    session.flush()
    calculate_indicators_for_symbol_timeframe(session, symbol, timeframe)

    despues = _valores(session, symbol, timeframe, tiempos)
    velas = tuple(
        ReparacionVela(t, antes[t][0], despues[t][0], antes[t][1], despues[t][1])
        for t in sorted(tiempos)
        if t in antes and t in despues
    )
    restantes = len(
        [1 for s, tf, _ in velas_vencidas_sin_cerrar(session, ahora=ahora, symbols=[symbol]) if tf == timeframe]
    )
    return ReparacionSerie(symbol, timeframe, velas, restantes, rsi_antes, _ultimo_rsi(session, symbol, timeframe, ahora=ahora))
