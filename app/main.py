"""Punto de entrada del orquestador.

Comandos disponibles en esta etapa:

- `healthcheck`: confirma que la infraestructura base funciona
  (configuración válida, conexión a la base de datos, extensión
  TimescaleDB instalada).
- `fetch-candles`: descarga velas reales desde Binance mainnet y las
  imprime. Es el "primer script" de la Fase 0 del plan ("descargar 100
  velas de BTC/USDT e imprimirlas") — todavía no persiste nada en la
  base de datos; eso es el siguiente bloque (Paso 3 — Ingesta completa).

No hay todavía ciclo de análisis, indicadores, ni ejecución de órdenes
(ver sección 16 del plan: "Primer entregable técnico", y sección 29:
"no sobreimplementación").

Uso:
    python -m app.main healthcheck
    python -m app.main fetch-candles BTCUSDT 1d 100
"""

from __future__ import annotations

import sys

from app.config import get_settings
from app.data.exchange_client import (
    InvalidSymbolError,
    InvalidTimeframeError,
    fetch_candles,
)
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.logging_config import configure_logging
from app.repositories.health import check_database_health

import logging

logger = logging.getLogger(__name__)


def run_healthcheck() -> int:
    """Ejecuta el health check y devuelve un código de salida (0 = OK, 1 = fallo)."""
    settings = get_settings()
    engine = get_engine(settings)
    session_factory = get_sessionmaker(engine)

    with session_scope(session_factory) as session:
        result = check_database_health(session)

    if result.database_reachable and result.timescaledb_installed:
        logger.info("healthcheck OK", extra={"component": "main.healthcheck"})
        print(f"OK: {result.detail}")
        return 0

    logger.error(
        "healthcheck FAILED",
        extra={
            "component": "main.healthcheck",
            "database_reachable": result.database_reachable,
            "timescaledb_installed": result.timescaledb_installed,
        },
    )
    print(f"FALLO: {result.detail}")
    return 1


def run_fetch_candles(symbol: str, timeframe: str, limit: int) -> int:
    """Descarga `limit` velas reales de Binance mainnet y las imprime.

    Solo lectura, sin persistencia todavía: es el script mínimo de la
    Fase 0 del plan, no el pipeline de ingesta completo (eso vendrá con
    `scripts/backfill_candles.py` y su propia lógica de idempotencia).
    """
    try:
        candles = fetch_candles(symbol, timeframe, limit=limit)
    except (InvalidSymbolError, InvalidTimeframeError, ValueError) as exc:
        logger.error(
            "fetch-candles FAILED",
            extra={"component": "main.fetch_candles", "symbol": symbol, "timeframe": timeframe},
        )
        print(f"FALLO: {exc}")
        return 1

    for candle in candles:
        print(
            f"{candle['open_time'].isoformat()}  "
            f"O={candle['open']}  H={candle['high']}  L={candle['low']}  "
            f"C={candle['close']}  V={candle['volume']}"
        )

    logger.info(
        "fetch-candles OK",
        extra={
            "component": "main.fetch_candles",
            "symbol": symbol,
            "timeframe": timeframe,
            "count": len(candles),
        },
    )
    print(f"\nOK: {len(candles)} velas de {symbol} ({timeframe}) descargadas desde Binance mainnet.")
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    argv = argv if argv is not None else sys.argv[1:]

    usage = "Uso: python -m app.main healthcheck | fetch-candles <SYMBOL> <TIMEFRAME> <LIMIT>"

    if not argv:
        print(usage)
        return 2

    if argv[0] == "healthcheck":
        return run_healthcheck()

    if argv[0] == "fetch-candles":
        if len(argv) != 4:
            print(usage)
            return 2
        _, symbol, timeframe, limit_str = argv
        try:
            limit = int(limit_str)
        except ValueError:
            print(f"FALLO: <LIMIT> debe ser un entero, recibido {limit_str!r}")
            return 2
        return run_fetch_candles(symbol, timeframe, limit)

    print(usage)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
