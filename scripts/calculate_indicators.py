"""Cálculo masivo de indicadores (sección 15.10 del plan, Paso 4).

Calcula y guarda los indicadores deterministas de la sección 6.1 (RSI-14,
ATR-14, SMA-90/120/180, volumen relativo, y en diaria además distancia al
ATH y al máximo de 90 días) a partir de las velas YA guardadas en la base
de datos — no descarga nada de Binance, eso ya lo hace
`scripts/backfill_candles.py`.

Uso:
    python -m scripts.calculate_indicators
    python -m scripts.calculate_indicators --symbols BTCUSDT,ETHUSDT --timeframes 1d

Por defecto corre sobre los 20 símbolos de la whitelist en las 4
temporalidades decididas (sección 6.1) — 80 combinaciones. Es idempotente
(misma restricción única que las velas): correr esto dos veces no duplica
ningún valor de indicador.
"""

from __future__ import annotations

import argparse
import logging

from app.config import get_settings
from app.data.exchange_client import SUPPORTED_TIMEFRAMES
from app.data.indicator_pipeline import calculate_indicators_for_all
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.symbols import ALLOWED_SYMBOLS
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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

    settings = get_settings()
    engine = get_engine(settings)
    session_factory = get_sessionmaker(engine)

    print(f"Calculando indicadores para {len(symbols)} símbolos x {len(timeframes)} temporalidades...")

    with session_scope(session_factory) as session:
        results = calculate_indicators_for_all(session, symbols, timeframes)

    total_inserted = 0
    sin_velas: list[str] = []
    for result in results:
        if result.candles_used == 0:
            sin_velas.append(f"{result.symbol} {result.timeframe}")
            continue
        print(
            f"{result.symbol:10s} {result.timeframe:4s}  "
            f"velas={result.candles_used:6d}  calculados={result.calculated:6d}  "
            f"insertados={result.inserted:6d}"
        )
        total_inserted += result.inserted

    if sin_velas:
        print(f"\nSin velas guardadas (se saltaron, correr primero el backfill): {', '.join(sin_velas)}")
        logger.warning(
            "calculate-indicators: combinaciones sin velas",
            extra={"component": "scripts.calculate_indicators", "combinaciones": sin_velas},
        )

    print(f"\nOK: {total_inserted} valores de indicadores nuevos insertados en total.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
