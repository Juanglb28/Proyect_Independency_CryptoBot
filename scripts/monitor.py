"""Monitor de posiciones abiertas del motor de ejecución paper (Paso 8).

Corre `app.execution.monitor.run_monitor_tick`: reconcilia el estado, vigila
las posiciones abiertas con el precio público de Binance, ejecuta stop / TP
parcial / trailing, calcula el equity y registra la pausa por drawdown. Python
determinista, sin IA (principio P2). Mueve solo dinero SIMULADO.

Uso:
    python -m scripts.monitor                  # un chequeo y termina
    python -m scripts.monitor --loop           # un chequeo cada 60 s hasta Ctrl+C

Con Docker (el PC debe estar encendido: el monitor solo vigila mientras corre):
    docker compose run --rm app python -m scripts.monitor --loop

Si el monitor estuvo apagado, el primer chequeo al volver revisa las
posiciones con el precio ACTUAL y aplica las reglas de salida de inmediato.

Códigos de salida (modo de un chequeo): 0 = bien; 1 = faltó un precio o una
posición dio error; 2 = la reconciliación encontró discrepancias y el monitor
se detuvo sin actuar (hay que revisarlas a mano). En `--loop` el monitor sigue
tras un fallo de precio o de base de datos y SOLO se detiene (código 2) ante una
discrepancia de reconciliación.
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.execution.monitor import ReconciliationBlocked, ResultadoTick, run_monitor_tick
from app.execution.price_source import BinanceTickerPriceSource, PriceSource
from app.logging_config import configure_logging

# Plan, sección 9: "cada 60 segundos".
INTERVALO_SEGUNDOS = 60

logger = logging.getLogger(__name__)


def format_tick(result: ResultadoTick, now: datetime) -> list[str]:
    """Líneas legibles de un chequeo (solo presentación)."""
    sello = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    if result.equity is not None:
        resumen = (
            f"{sello}  equity={result.equity} USD  pico={result.peak_equity} USD  "
            f"drawdown={result.drawdown_pct}%  abiertas={result.posiciones_abiertas}"
        )
    else:
        resumen = f"{sello}  equity=no calculada  abiertas={result.posiciones_abiertas}"
    lineas = [resumen]
    for c in result.chequeos:
        if c.intencion is not None:
            lineas.append(
                f"  SALIDA {c.intencion.value}: {c.symbol} al precio {c.observed_price}  orden={c.orden_id}"
            )
    if result.expiradas:
        lineas.append(f"  Entradas pendientes expiradas: {result.expiradas}")
    if result.resultados_reparados:
        lineas.append(f"  Resultados de operaciones cerradas generados por reparación: {result.resultados_reparados}")
    if result.pausa_registrada:
        lineas.append(
            "  *** PAUSA POR DRAWDOWN: el sistema no abrirá entradas nuevas hasta que lo reanudes "
            "(python -m scripts.system_control resume). Las posiciones siguen vigiladas. ***"
        )
    for simbolo in result.sin_precio:
        lineas.append(f"  AVISO: sin precio válido para {simbolo}; no se evaluó en este chequeo.")
    for e in result.errores:
        lineas.append(f"  ERROR en {e.symbol}: {e.mensaje}")
    for nota in result.notas:
        lineas.append(f"  {nota}")
    return lineas


def format_blocked(error: ReconciliationBlocked) -> list[str]:
    lineas = [
        f"RECONCILIACIÓN: {len(error.discrepancias)} discrepancia(s). El monitor se detiene SIN actuar; "
        "revisa y corrige a mano antes de volver a arrancarlo:"
    ]
    lineas.extend(f"  [{d.codigo.value}] {d.detalle}" for d in error.discrepancias)
    return lineas


def run_once(
    session_factory: sessionmaker[Session],
    *,
    price_source: PriceSource,
    initial_capital: Decimal,
    now: datetime,
) -> tuple[list[str], int]:
    """Un chequeo en su propia transacción. Devuelve `(líneas, código)`."""
    try:
        with session_scope(session_factory) as session:
            result = run_monitor_tick(session, now=now, price_source=price_source, initial_capital=initial_capital)
    except ReconciliationBlocked as exc:
        return format_blocked(exc), 2
    return format_tick(result, now), (1 if result.hubo_problemas else 0)


def run_loop(
    session_factory: sessionmaker[Session],
    *,
    price_source: PriceSource,
    initial_capital: Decimal,
    interval: float = INTERVALO_SEGUNDOS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    max_ticks: int | None = None,
    emit: Callable[[str], None] = print,
) -> int:
    """Repite el chequeo cada `interval` segundos. Se detiene (código 2) solo
    ante una discrepancia de reconciliación; cualquier otro fallo del
    chequeo se registra y se reintenta en el siguiente (P5: el sistema puede
    caerse y revivir sin perder estado, que vive en la base)."""
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        inicio = time.monotonic()
        now = clock()
        try:
            lineas, codigo = run_once(
                session_factory, price_source=price_source, initial_capital=initial_capital, now=now
            )
        except Exception:  # noqa: BLE001 - un fallo (p. ej. la base caída) no mata el monitor
            logger.error("monitor: el chequeo falló; se reintenta en el siguiente", exc_info=True)
            lineas, codigo = [f"{now:%Y-%m-%d %H:%M:%S UTC}  ERROR: el chequeo falló (ver el log); se reintenta."], 1
        for linea in lineas:
            emit(linea)
        if codigo == 2:
            return 2
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        sleep(max(0.0, interval - (time.monotonic() - inicio)))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--loop", action="store_true", help="Repite el chequeo cada 60 s hasta Ctrl+C.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = parse_args(argv)
    settings = get_settings()
    session_factory = get_sessionmaker(get_engine(settings))
    price_source = BinanceTickerPriceSource()

    if not args.loop:
        lineas, codigo = run_once(
            session_factory,
            price_source=price_source,
            initial_capital=settings.paper_capital_usd,
            now=datetime.now(timezone.utc),
        )
        for linea in lineas:
            print(linea)
        return codigo

    print(f"Monitor activo: un chequeo cada {INTERVALO_SEGUNDOS} s. Ctrl+C para detenerlo.")
    try:
        return run_loop(session_factory, price_source=price_source, initial_capital=settings.paper_capital_usd)
    except KeyboardInterrupt:
        print("Monitor detenido.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
