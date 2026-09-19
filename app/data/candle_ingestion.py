"""Orquesta la descarga paginada de velas y su persistencia idempotente
(sección 15.10 del plan, Paso 3: "descarga histórica, actualización
incremental y pruebas de integridad").

No valida símbolos ni temporalidades por su cuenta — eso ya lo hace
`exchange_client.fetch_candles` — y no calcula nada. Esta capa solo sabe
avanzar en el tiempo, página por página, hasta cubrir la ventana pedida.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.data.exchange_client import Candle, fetch_candles, get_exchange
from app.repositories.candles import save_candles

# Duración de cada vela por temporalidad — necesario para saber a partir de
# qué instante pedir la siguiente página sin re-pedir la última vela ya
# recibida (lo que produciría un bucle infinito).
_TIMEFRAME_DURATIONS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
}

# Cota de seguridad contra un bucle infinito por un bug de paginación. Un
# backfill de 2 años en 1h son ~17.520 velas / 1000 por página ≈ 18
# páginas; 500 páginas da margen amplio sin dejar la puerta abierta a un
# bucle que no termine nunca.
_MAX_PAGES_SAFETY = 500

FetchFn = Callable[..., list[Candle]]


@dataclass(frozen=True)
class BackfillResult:
    """Resumen de una corrida de backfill para un (symbol, timeframe)."""

    symbol: str
    timeframe: str
    fetched: int
    inserted: int
    pages: int
    last_open_time: datetime | None
    hit_page_safety_limit: bool


def backfill_symbol_timeframe(
    session: Session,
    symbol: str,
    timeframe: str,
    since: datetime,
    until: datetime | None = None,
    *,
    fetch_fn: FetchFn = fetch_candles,
    exchange: object | None = None,
    page_limit: int = 1000,
) -> BackfillResult:
    """Descarga y guarda velas de `symbol`/`timeframe` desde `since` hasta `until`.

    Idempotente: se puede volver a correr con el mismo `since` después de
    una interrupción — `save_candles` ignora las velas que ya estén
    guardadas, así que nunca duplica (sección 15.7 y P5 del plan).

    `fetch_fn` es inyectable para poder probar la lógica de paginación sin
    red real (ver tests/unit/test_candle_ingestion.py); en producción es
    siempre `exchange_client.fetch_candles`.
    """
    if timeframe not in _TIMEFRAME_DURATIONS:
        raise ValueError(
            f"Timeframe no soportado para backfill: {timeframe!r}. "
            f"Usar una de {tuple(_TIMEFRAME_DURATIONS)}"
        )
    if since.tzinfo is None:
        raise ValueError("`since` debe ser un datetime con tzinfo (UTC), no naive")

    until = until if until is not None else datetime.now(timezone.utc)
    if until.tzinfo is None:
        raise ValueError("`until` debe ser un datetime con tzinfo (UTC), no naive")

    # Un único cliente para todas las páginas de esta corrida, aunque el
    # llamador no haya pasado uno: si cada página creara su propia
    # instancia de ccxt, el rate-limiting interno (`enableRateLimit`) no
    # tendría memoria entre páginas y podría disparar un 429 de Binance
    # incluso dentro del backfill de un solo símbolo/temporalidad.
    if exchange is None:
        exchange = get_exchange()

    step = _TIMEFRAME_DURATIONS[timeframe]

    cursor = since
    total_fetched = 0
    total_inserted = 0
    pages = 0
    last_open_time: datetime | None = None
    hit_safety_limit = False

    while cursor < until:
        if pages >= _MAX_PAGES_SAFETY:
            hit_safety_limit = True
            break

        candles = fetch_fn(symbol, timeframe, limit=page_limit, since=cursor, exchange=exchange)
        pages += 1

        if not candles:
            # Binance no tiene más datos para esta ventana (p. ej. el
            # símbolo no existía todavía en `cursor`). No es un error.
            break

        total_fetched += len(candles)
        total_inserted += save_candles(session, candles)
        last_open_time = candles[-1]["open_time"]
        cursor = last_open_time + step

        if len(candles) < page_limit:
            # Página parcial: ya alcanzamos el presente, no hay más páginas.
            break

    return BackfillResult(
        symbol=symbol,
        timeframe=timeframe,
        fetched=total_fetched,
        inserted=total_inserted,
        pages=pages,
        last_open_time=last_open_time,
        hit_page_safety_limit=hit_safety_limit,
    )


def backfill_all(
    session: Session,
    symbols: Iterable[str],
    timeframes: Iterable[str],
    since: datetime,
    *,
    until: datetime | None = None,
) -> list[BackfillResult]:
    """Corre `backfill_symbol_timeframe` para cada combinación de símbolo y
    temporalidad, reutilizando un único cliente ccxt (ver docstring de
    `exchange_client.fetch_candles` sobre por qué importa reutilizarlo) y
    haciendo commit después de cada combinación — no al final de todas —
    para que una interrupción a mitad de camino no pierda el progreso ya
    guardado (P5 del plan: el sistema debe poder morir y revivir).
    """
    exchange = get_exchange()
    results: list[BackfillResult] = []

    for symbol in symbols:
        for timeframe in timeframes:
            result = backfill_symbol_timeframe(
                session, symbol, timeframe, since, until=until, exchange=exchange
            )
            results.append(result)
            session.commit()

    return results
