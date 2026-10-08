"""Backfill histórico de velas (sección 15.10 del plan, Paso 3).

Descarga velas reales desde Binance mainnet para los símbolos y
temporalidades pedidos, y las guarda en la tabla `velas` de forma
idempotente (correr esto dos veces no duplica nada).

La vela del período en curso se guarda como NO cerrada y se refresca en cada
corrida hasta que cierra; después es inmutable. Si quedan velas vencidas sin
cerrar (datos parciales antiguos) el script termina con código 3.

Uso:
    python -m scripts.backfill_candles
    python -m scripts.backfill_candles --days 30
    python -m scripts.backfill_candles --symbols BTCUSDT,ETHUSDT --timeframes 1d

Por defecto descarga ~2 años (sección 3 del plan) para los 20 símbolos de
la whitelist en las 4 temporalidades decididas (sección 6.1). Eso son 80
combinaciones símbolo/temporalidad y, para 1h, unas 18 páginas cada una:
puede tardar varios minutos por el rate-limiting de Binance. `--days` y
`--symbols`/`--timeframes` sirven para probar rápido con una ventana o un
subconjunto más chicos antes de lanzar la corrida completa.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.data.candle_ingestion import backfill_all, velas_vencidas_sin_cerrar
from app.data.exchange_client import SUPPORTED_TIMEFRAMES
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.symbols import ALLOWED_SYMBOLS
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)

_DEFAULT_DAYS = 730  # ~2 años, sección 3 del plan


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--days",
        type=int,
        default=_DEFAULT_DAYS,
        help=f"Días hacia atrás a descargar (default: {_DEFAULT_DAYS}, ~2 años).",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="Símbolos separados por coma (default: los 20 de la whitelist).",
    )
    parser.add_argument(
        "--timeframes",
        type=str,
        default=None,
        help="Temporalidades separadas por coma (default: 1h,4h,12h,1d).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = parse_args(argv)

    symbols = tuple(s.strip() for s in args.symbols.split(",")) if args.symbols else ALLOWED_SYMBOLS
    timeframes = (
        tuple(t.strip() for t in args.timeframes.split(",")) if args.timeframes else SUPPORTED_TIMEFRAMES
    )
    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    settings = get_settings()
    engine = get_engine(settings)
    session_factory = get_sessionmaker(engine)

    print(f"Backfill desde {since.isoformat()} para {len(symbols)} símbolos x {len(timeframes)} temporalidades...")

    ahora = datetime.now(timezone.utc)
    with session_scope(session_factory) as session:
        results = backfill_all(session, symbols, timeframes, since, now=ahora)
        vencidas = velas_vencidas_sin_cerrar(session, ahora=ahora, symbols=symbols)

    total_inserted = 0
    for result in results:
        flag = "  [LÍMITE DE PÁGINAS ALCANZADO]" if result.hit_page_safety_limit else ""
        print(
            f"{result.symbol:10s} {result.timeframe:4s}  "
            f"fetched={result.fetched:6d}  inserted={result.inserted:6d}  "
            f"updated={result.updated:4d}  en_curso={result.open_candles:2d}  "
            f"pages={result.pages:3d}{flag}"
        )
        total_inserted += result.inserted
        if result.hit_page_safety_limit:
            logger.warning(
                "backfill hit page safety limit",
                extra={
                    "component": "scripts.backfill_candles",
                    "symbol": result.symbol,
                    "timeframe": result.timeframe,
                    "pages": result.pages,
                },
            )

    print(f"\nOK: {total_inserted} velas nuevas insertadas en total.")
    if vencidas:
        print(
            f"\nERROR: {len(vencidas)} vela(s) quedaron guardadas como NO cerradas aunque su hora de cierre ya pasó "
            "(valores parciales de antes de la corrección del 2026-10-08, o fuera de la ventana descargada). "
            "No se debe analizar sobre ellas: corre `python -m scripts.repair_partial_candles` (simulación) y luego "
            "con `--ejecutar`.",
            file=sys.stderr,
        )
        for symbol, timeframe, open_time in vencidas[:10]:
            print(f"  {symbol} {timeframe} {open_time.isoformat()}", file=sys.stderr)
        if len(vencidas) > 10:
            print(f"  ... y {len(vencidas) - 10} más", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
