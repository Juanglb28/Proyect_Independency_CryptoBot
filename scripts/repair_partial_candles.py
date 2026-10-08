"""Repara las velas que quedaron congeladas a medias (2026-10-08).

Hasta la corrección, la ingesta guardaba la vela del período EN CURSO y la
dejaba congelada con los valores del momento de la descarga. La migración marca
esas velas como NO cerradas; este script las vuelve a descargar de Binance
(mainnet, solo lectura), las corrige y recalcula los indicadores derivados.

Por defecto es una SIMULACIÓN: ejecuta exactamente el mismo camino de código y
al final DESHACE todo (rollback); solo muestra qué cambiaría. Con --ejecutar
confirma los cambios, una serie (símbolo + temporalidad) por vez: si se
interrumpe, lo ya confirmado queda y se puede volver a correr.

Uso:
    python -m scripts.repair_partial_candles              # simulación
    python -m scripts.repair_partial_candles --ejecutar   # aplica

Las decisiones ya tomadas NO se tocan: cada propuesta guarda su propia copia
congelada de los datos con los que se decidió (decision snapshot).

Código de salida: 0 = todo reparado (o nada que reparar); 1 = quedaron velas
vencidas sin reparar (Binance no las devolvió).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal

from app.config import get_settings
from app.data.candle_ingestion import velas_vencidas_sin_cerrar
from app.data.candle_repair import ReparacionSerie, reparar_serie
from app.data.exchange_client import get_exchange
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.symbols import ALLOWED_SYMBOLS
from app.logging_config import configure_logging


def formatear_serie(r: ReparacionSerie) -> list[str]:
    cambios = [v.cambio_close_pct for v in r.velas if v.cambio_close_pct is not None]
    peor = max((abs(c) for c in cambios), default=Decimal(0))
    rsi = ""
    if r.rsi_antes is not None and r.rsi_despues is not None:
        rsi = f"  RSI14 {r.rsi_antes:.2f} -> {r.rsi_despues:.2f}"
    resto = f"  SIN REPARAR: {r.restantes}" if r.restantes else ""
    return [f"{r.symbol:10s} {r.timeframe:4s}  velas={len(r.velas):3d}  mayor cambio de cierre={peor:.2f}%{rsi}{resto}"]


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ejecutar", action="store_true", help="Confirma los cambios (por defecto solo simula).")
    args = parser.parse_args(argv)

    settings = get_settings()
    session_factory = get_sessionmaker(get_engine(settings))
    ahora = datetime.now(timezone.utc)

    with session_scope(session_factory) as session:
        pendientes = sorted({(s, tf) for s, tf, _ in velas_vencidas_sin_cerrar(session, ahora=ahora)})
    if not pendientes:
        print("No hay velas vencidas sin cerrar: nada que reparar.")
        return 0

    modo = "EJECUCIÓN" if args.ejecutar else "SIMULACIÓN (no se guarda nada; usa --ejecutar para aplicar)"
    print(f"{modo}: {len(pendientes)} serie(s) con velas congeladas a medias.")
    exchange = get_exchange()
    sin_reparar = 0
    total = 0
    for symbol, timeframe in pendientes:
        if symbol not in ALLOWED_SYMBOLS:
            print(f"{symbol} {timeframe}: fuera de la lista autorizada; se omite.")
            continue
        with session_scope(session_factory) as session:
            resultado = reparar_serie(session, symbol, timeframe, ahora=ahora, exchange=exchange)
            if resultado is None:
                continue
            for linea in formatear_serie(resultado):
                print(linea)
            total += len(resultado.velas)
            sin_reparar += resultado.restantes
            if not args.ejecutar:
                session.rollback()
    print(f"\n{'Reparadas' if args.ejecutar else 'Se repararían'}: {total} vela(s).")
    if sin_reparar:
        print(f"ERROR: {sin_reparar} vela(s) siguen vencidas sin cerrar.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
