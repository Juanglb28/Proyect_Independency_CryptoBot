"""Verificación formal de indicadores ya calculados (sección 22/23 del plan,
"tests futuros obligatorios" — reproducibilidad, ausencia de valores
corruptos).

Formaliza en código lo que hasta ahora era una comparación manual: para
cada combinación símbolo/temporalidad, recalcula los indicadores a partir
de las velas guardadas y los compara contra lo que quedó persistido en la
tabla `indicadores`, además de chequear invariantes matemáticos (RSI entre
0 y 100, ATR no negativo, etc.).

IMPORTANTE — lo que este script NO hace: no vuelve a comparar contra
TradingView. Ver el docstring de `app/data/indicator_verification.py` para
el porqué (esa validación ya se hizo a mano y automatizarla exigiría una
fuente externa que hoy no está definida — sección P3 del plan, cero datos
inventados).

Uso:
    python -m scripts.verify_indicators
    python -m scripts.verify_indicators --symbols BTCUSDT,ETHUSDT --timeframes 1d

Por defecto corre sobre los 20 símbolos de la whitelist en las 4
temporalidades. Es de solo lectura: no modifica la base de datos. Termina
con código de salida 1 si encuentra alguna discrepancia (útil para correrlo
como chequeo antes de avanzar a la siguiente fase).
"""

from __future__ import annotations

import argparse
import logging

from app.config import get_settings
from app.data.exchange_client import SUPPORTED_TIMEFRAMES
from app.data.indicator_verification import verify_all
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.symbols import ALLOWED_SYMBOLS
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)

# Cuántas discrepancias imprimir en detalle por combinación antes de
# resumir el resto con un "... y N más": una combinación completamente rota
# (por ejemplo, tras correr con código viejo) puede tener miles de
# discrepancias, y listarlas todas ahogaría el resto del reporte.
_MAX_DETALLES_POR_COMBINACION = 10


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

    print(f"Verificando indicadores de {len(symbols)} símbolos x {len(timeframes)} temporalidades...")

    with session_scope(session_factory) as session:
        results = verify_all(session, symbols, timeframes)

    sin_velas: list[str] = []
    combinaciones_sucias = 0
    total_mismatches = 0
    total_bounds = 0

    for result in results:
        if result.candles_used == 0:
            sin_velas.append(f"{result.symbol} {result.timeframe}")
            continue

        if result.is_clean:
            print(
                f"OK   {result.symbol:10s} {result.timeframe:4s}  "
                f"velas={result.candles_used:6d}  indicadores={result.persisted_count:6d}"
            )
            continue

        combinaciones_sucias += 1
        total_mismatches += len(result.mismatches)
        total_bounds += len(result.bounds_violations)
        print(
            f"MAL  {result.symbol:10s} {result.timeframe:4s}  "
            f"esperados={result.expected_count:6d}  persistidos={result.persisted_count:6d}  "
            f"discrepancias={len(result.mismatches):4d}  invariantes_rotos={len(result.bounds_violations):4d}"
        )
        for mismatch in result.mismatches[:_MAX_DETALLES_POR_COMBINACION]:
            print(
                f"       {mismatch.kind:14s} {mismatch.calculated_at} {mismatch.indicator_name:24s} "
                f"esperado={mismatch.expected} persistido={mismatch.actual}"
            )
        if len(result.mismatches) > _MAX_DETALLES_POR_COMBINACION:
            print(f"       ... y {len(result.mismatches) - _MAX_DETALLES_POR_COMBINACION} discrepancias más")
        for violation in result.bounds_violations[:_MAX_DETALLES_POR_COMBINACION]:
            print(
                f"       invariante_roto {violation.calculated_at} {violation.indicator_name:24s} "
                f"valor={violation.value}  ({violation.reason})"
            )
        if len(result.bounds_violations) > _MAX_DETALLES_POR_COMBINACION:
            print(f"       ... y {len(result.bounds_violations) - _MAX_DETALLES_POR_COMBINACION} invariantes rotos más")

        logger.warning(
            "verify-indicators: combinación con discrepancias",
            extra={
                "component": "scripts.verify_indicators",
                "symbol": result.symbol,
                "timeframe": result.timeframe,
                "mismatches": len(result.mismatches),
                "bounds_violations": len(result.bounds_violations),
            },
        )

    if sin_velas:
        print(f"\nSin velas guardadas (se saltaron): {', '.join(sin_velas)}")

    if combinaciones_sucias:
        print(
            f"\nFALLÓ: {combinaciones_sucias} combinación(es) con discrepancias "
            f"({total_mismatches} valores distintos/faltantes, {total_bounds} invariantes rotos)."
        )
        return 1

    print("\nOK: todas las combinaciones con velas guardadas verifican limpio.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
