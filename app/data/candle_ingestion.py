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
from app.domain.rules.candles import DURACION_POR_TEMPORALIDAD, esta_cerrada
from app.repositories.candles import list_velas_sin_cerrar, upsert_candles
from app.repositories.indicators import delete_indicators_from

# Duración de cada vela por temporalidad — necesario para saber a partir de
# qué instante pedir la siguiente página sin re-pedir la última vela ya
# recibida (lo que produciría un bucle infinito).
_TIMEFRAME_DURATIONS: dict[str, timedelta] = DURACION_POR_TEMPORALIDAD

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
    # Velas que estaban guardadas EN CURSO (o parciales) y se refrescaron.
    updated: int = 0
    # Velas que quedaron guardadas como NO cerradas (la vela en curso de ahora).
    open_candles: int = 0


def anotar_cierre(candles: list[Candle], *, ahora: datetime) -> list[Candle]:
    """Copia de `candles` con `cerrada` anotada. Una vela es definitiva si hay
    otra posterior en la misma descarga o si ya pasó su hora de cierre más la
    gracia (`app.domain.rules.candles`); la última vela de una descarga hecha a
    mitad del período queda como NO cerrada."""
    anotadas: list[Candle] = []
    for indice, candle in enumerate(candles):
        cerrada = esta_cerrada(
            candle["open_time"],
            candle["timeframe"],
            ahora=ahora,
            hay_vela_posterior=indice < len(candles) - 1,
        )
        anotadas.append({**candle, "cerrada": cerrada})
    return anotadas


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
    now: datetime | None = None,
) -> BackfillResult:
    """Descarga y guarda velas de `symbol`/`timeframe` desde `since` hasta `until`.

    Idempotente: se puede volver a correr con el mismo `since` después de
    una interrupción — `save_candles` ignora las velas que ya estén
    guardadas, así que nunca duplica (sección 15.7 y P5 del plan).

    Velas en curso: Binance devuelve también la vela del período que aún no
    cerró. Se guarda marcada `cerrada=False` y se REFRESCA en cada corrida hasta
    que cierra (después queda inmutable); nunca se congela con valores parciales.
    Si una vela ya guardada cambia, los indicadores derivados de ella y de las
    posteriores se descartan para que se recalculen desde velas cerradas.
    `now` (inyectable en pruebas) es el instante contra el que se decide si una
    vela cerró.

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

    ahora = now if now is not None else datetime.now(timezone.utc)
    if ahora.tzinfo is None:
        raise ValueError("`now` debe ser un datetime con tzinfo (UTC), no naive")
    until = until if until is not None else ahora
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
    total_updated = 0
    open_candles = 0
    primera_actualizada: datetime | None = None
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
        anotadas = anotar_cierre(candles, ahora=ahora)
        resultado = upsert_candles(session, anotadas)
        total_inserted += resultado.insertadas
        total_updated += len(resultado.actualizadas)
        open_candles += sum(1 for c in anotadas if not c["cerrada"])
        if resultado.actualizadas and (primera_actualizada is None or resultado.actualizadas[0] < primera_actualizada):
            primera_actualizada = resultado.actualizadas[0]
        last_open_time = candles[-1]["open_time"]
        cursor = last_open_time + step

        if len(candles) < page_limit:
            # Página parcial: ya alcanzamos el presente, no hay más páginas.
            break

    if primera_actualizada is not None:
        # Una vela guardada cambió de valores: lo derivado de ella (y, por la
        # recursión de Wilder y las medias, de las posteriores) ya no vale.
        delete_indicators_from(session, symbol=symbol, timeframe=timeframe, calculated_at_from=primera_actualizada)

    return BackfillResult(
        symbol=symbol,
        timeframe=timeframe,
        fetched=total_fetched,
        inserted=total_inserted,
        pages=pages,
        last_open_time=last_open_time,
        hit_page_safety_limit=hit_safety_limit,
        updated=total_updated,
        open_candles=open_candles,
    )


def backfill_all(
    session: Session,
    symbols: Iterable[str],
    timeframes: Iterable[str],
    since: datetime,
    *,
    until: datetime | None = None,
    now: datetime | None = None,
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
                session, symbol, timeframe, since, until=until, exchange=exchange, now=now
            )
            results.append(result)
            session.commit()

    return results


def velas_vencidas_sin_cerrar(
    session: Session, *, ahora: datetime, symbols: Iterable[str] | None = None
) -> list[tuple[str, str, datetime]]:
    """Velas guardadas como NO cerradas cuya hora de cierre ya pasó: datos
    parciales que nadie refrescó (el caso típico son las velas congeladas antes
    de la corrección del 2026-10-08, ver `scripts/repair_partial_candles.py`).
    La vela en curso de ahora NO cuenta."""
    permitidos = None if symbols is None else set(symbols)
    return [
        (symbol, timeframe, open_time)
        for symbol, timeframe, open_time in list_velas_sin_cerrar(session)
        if (permitidos is None or symbol in permitidos) and esta_cerrada(open_time, timeframe, ahora=ahora)
    ]
