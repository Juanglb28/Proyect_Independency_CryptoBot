"""Punto de entrada del orquestador.

Comandos disponibles en esta etapa:

- `healthcheck`: confirma que la infraestructura base funciona
  (configuración válida, conexión a la base de datos, extensión
  TimescaleDB instalada).
- `fetch-candles`: descarga velas reales desde Binance mainnet y las
  imprime. Es el "primer script" de la Fase 0 del plan ("descargar 100
  velas de BTC/USDT e imprimirlas") — no persiste nada en la base de datos
  (eso lo hace `scripts/backfill_candles.py`).
- `calculate-indicators`: calcula y guarda los indicadores deterministas
  de la sección 6.1 del plan (RSI-14, ATR-14, SMA-90/120/180, volumen
  relativo, y en diaria además distancia al ATH y al máximo de 90 días)
  para un símbolo/temporalidad ya ingerido, e imprime el último valor de
  cada uno para poder compararlo a mano contra TradingView (criterio de
  aceptación de la Fase 1: "Hecho cuando: tus números coinciden con
  TradingView").

No hay todavía ciclo de análisis completo, prefiltro ni ejecución de
órdenes (ver sección 16 del plan: "Primer entregable técnico", y sección
29: "no sobreimplementación").

Uso:
    python -m app.main healthcheck
    python -m app.main fetch-candles BTCUSDT 1d 100
    python -m app.main calculate-indicators BTCUSDT 1d
"""

from __future__ import annotations

import sys

from app.config import get_settings
from app.data.exchange_client import (
    SUPPORTED_TIMEFRAMES,
    InvalidSymbolError,
    InvalidTimeframeError,
    fetch_candles,
)
from app.data.indicator_calculator import calculate_indicators
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.symbols import ALLOWED_SYMBOLS
from app.logging_config import configure_logging
from app.repositories.candles import get_candles
from app.repositories.health import check_database_health
from app.repositories.indicators import save_indicators

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


def run_calculate_indicators(symbol: str, timeframe: str) -> int:
    """Calcula y guarda los indicadores de `symbol`/`timeframe` a partir de
    las velas ya guardadas en la base de datos (no descarga nada de
    Binance — eso ya lo hizo `scripts/backfill_candles.py`).

    Imprime el último valor de cada indicador para que se pueda comparar a
    mano contra TradingView, tal como exige la Fase 1 del plan antes de
    seguir avanzando.
    """
    if symbol not in ALLOWED_SYMBOLS:
        print(f"FALLO: símbolo no autorizado: {symbol!r}. Ver app/domain/symbols.py.")
        return 1
    if timeframe not in SUPPORTED_TIMEFRAMES:
        print(f"FALLO: temporalidad no soportada: {timeframe!r}. Usar una de {SUPPORTED_TIMEFRAMES}.")
        return 1

    settings = get_settings()
    engine = get_engine(settings)
    session_factory = get_sessionmaker(engine)

    with session_scope(session_factory) as session:
        candles = get_candles(session, symbol, timeframe)
        if not candles:
            print(
                f"FALLO: no hay velas guardadas de {symbol} ({timeframe}). "
                f"Corre primero: python -m scripts.backfill_candles"
            )
            return 1

        rows = calculate_indicators(symbol, timeframe, candles)
        inserted = save_indicators(session, rows)

    # Último valor de cada indicador (las filas ya vienen en el mismo orden
    # que las velas, es decir, ascendente por open_time).
    last_by_name = {}
    for row in rows:
        last_by_name[row.indicator_name] = row

    print(f"Indicadores de {symbol} ({timeframe}) — último valor calculado:\n")
    for name in sorted(last_by_name):
        row = last_by_name[name]
        print(f"  {name:22s} {row.value}   (vela de {row.calculated_at.isoformat()})")

    logger.info(
        "calculate-indicators OK",
        extra={
            "component": "main.calculate_indicators",
            "symbol": symbol,
            "timeframe": timeframe,
            "calculated": len(rows),
            "inserted": inserted,
        },
    )
    print(f"\nOK: {len(rows)} valores calculados, {inserted} nuevos insertados en la base de datos.")
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    argv = argv if argv is not None else sys.argv[1:]

    usage = (
        "Uso: python -m app.main healthcheck"
        " | fetch-candles <SYMBOL> <TIMEFRAME> <LIMIT>"
        " | calculate-indicators <SYMBOL> <TIMEFRAME>"
    )

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

    if argv[0] == "calculate-indicators":
        if len(argv) != 3:
            print(usage)
            return 2
        _, symbol, timeframe = argv
        return run_calculate_indicators(symbol, timeframe)

    print(usage)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
