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

Avisos (Paso 11a-2): el chequeo ESCRIBE los avisos en la tabla `alertas`
(caída >5 %/h, volumen >3σ, orden >200 USD pendiente, Binance caído o
restablecido, pausa por drawdown, discrepancia de reconciliación) y, después
de confirmar la transacción, este script envía por Telegram los que estén
pendientes. Si `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` no están configurados
los avisos quedan guardados y se envían cuando se configuren. Un fallo de
Telegram nunca detiene el monitor.

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
from app.execution.escalations import avisar_discrepancia
from app.execution.monitor import ReconciliationBlocked, ResultadoTick, run_monitor_tick
from app.execution.price_source import BinanceTickerPriceSource, PriceSource
from app.logging_config import configure_logging
from app.notifications.dispatcher import dispatch_pending_alerts
from app.notifications.telegram import Sender, build_sender

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
    for tipo in result.alertas_creadas:
        lineas.append(f"  AVISO registrado: {tipo.value}")
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


def _registrar_discrepancia(session_factory: sessionmaker[Session], error: ReconciliationBlocked, now: datetime) -> None:
    """Guarda el aviso de discrepancia en una transacción PROPIA (la del
    chequeo se revirtió). Nunca propaga un fallo: el monitor ya se detiene."""
    try:
        with session_scope(session_factory) as session:
            avisar_discrepancia(session, now=now, discrepancias=error.discrepancias)
    except Exception:  # noqa: BLE001
        logger.error("monitor: no se pudo guardar el aviso de discrepancia", exc_info=True)


def _enviar_avisos(session_factory: sessionmaker[Session], sender: Sender | None, now: datetime) -> list[str]:
    """Envía los avisos pendientes DESPUÉS del commit. Nunca propaga un fallo."""
    try:
        despacho = dispatch_pending_alerts(session_factory, sender, now=now)
    except Exception:  # noqa: BLE001 - Telegram no puede detener al monitor
        logger.error("monitor: el envío de avisos falló", exc_info=True)
        return ["  AVISOS: el envío falló (ver el log); se reintenta en el siguiente chequeo."]
    lineas = []
    if despacho.enviadas:
        lineas.append(f"  Avisos enviados por Telegram: {despacho.enviadas}")
    if despacho.fallidas:
        lineas.append(f"  Avisos que no se pudieron enviar (se reintentan): {despacho.fallidas}")
    return lineas


def run_once(
    session_factory: sessionmaker[Session],
    *,
    price_source: PriceSource,
    initial_capital: Decimal,
    now: datetime,
    sender: Sender | None = None,
) -> tuple[list[str], int]:
    """Un chequeo en su propia transacción y, ya confirmada, el envío de los
    avisos pendientes. Devuelve `(líneas, código)`."""
    try:
        with session_scope(session_factory) as session:
            result = run_monitor_tick(session, now=now, price_source=price_source, initial_capital=initial_capital)
    except ReconciliationBlocked as exc:
        _registrar_discrepancia(session_factory, exc, now)
        return format_blocked(exc) + _enviar_avisos(session_factory, sender, now), 2
    lineas = format_tick(result, now) + _enviar_avisos(session_factory, sender, now)
    return lineas, (1 if result.hubo_problemas else 0)


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
    sender: Sender | None = None,
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
                session_factory, price_source=price_source, initial_capital=initial_capital, now=now, sender=sender
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
    sender = build_sender(settings.telegram_bot_token, settings.telegram_chat_id)
    if sender is None:
        print("Telegram sin configurar: los avisos se guardan en la base y se enviarán cuando lo configures.")

    if not args.loop:
        lineas, codigo = run_once(
            session_factory,
            price_source=price_source,
            initial_capital=settings.paper_capital_usd,
            now=datetime.now(timezone.utc),
            sender=sender,
        )
        for linea in lineas:
            print(linea)
        return codigo

    print(f"Monitor activo: un chequeo cada {INTERVALO_SEGUNDOS} s. Ctrl+C para detenerlo.")
    try:
        return run_loop(
            session_factory, price_source=price_source, initial_capital=settings.paper_capital_usd, sender=sender
        )
    except KeyboardInterrupt:
        print("Monitor detenido.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
