"""Control manual del sistema de ejecución paper (Paso 8): estado y
reanudación tras una pausa por drawdown.

Uso:
    python -m scripts.system_control status    # pausa, PARAR, equity y pico vigentes
    python -m scripts.system_control resume    # reanuda tras una pausa por drawdown

La pausa por drawdown (-15% desde el pico de equity) la registra el monitor
solo y SOLO bloquea entradas nuevas; reanudar es siempre manual. Al reanudar,
el pico de equity se reinicia al equity de ese momento (el pico anterior
queda guardado como registro). Cada comando es una sola transacción.

El archivo `PARAR` (en la raíz del proyecto) frena las entradas nuevas sin
detener nada más; se crea y se borra a mano, no con este comando.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.execution.price_source import BinanceTickerPriceSource, PriceSource
from app.execution.system_control import ResultadoReanudacion, get_system_state, resume_system
from app.logging_config import configure_logging

_MENSAJES_REANUDACION = {
    ResultadoReanudacion.NO_PAUSADO: "El sistema no estaba pausado por drawdown; no se hizo nada.",
    ResultadoReanudacion.SIN_PRECIO: (
        "No se pudo obtener el precio de una posición abierta; no se reanudó (no se inventa un precio). "
        "Intenta de nuevo."
    ),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("status", help="Muestra el estado del sistema.")
    sub.add_parser("resume", help="Reanuda tras una pausa por drawdown.")
    return parser.parse_args(argv)


def run_command(
    session: Session,
    args: argparse.Namespace,
    *,
    price_source: PriceSource,
    now: datetime,
    initial_capital: Decimal,
) -> list[str]:
    """Ejecuta el comando y devuelve las líneas a imprimir (separado de `main`
    para probarlo sin red ni sesión propia)."""
    if args.comando == "status":
        estado = get_system_state(session, initial_capital=initial_capital)
        return [
            f"Pausado por drawdown: {'SÍ' if estado.pausado else 'no'}",
            f"Archivo PARAR: {'PRESENTE (no se abren entradas nuevas)' if estado.parada_emergencia else 'no'}",
            f"Posiciones abiertas: {estado.posiciones_abiertas}",
            f"Último equity registrado: {estado.ultimo_equity if estado.ultimo_equity is not None else 'ninguno todavía'}"
            + (f" ({estado.ultima_observacion_en:%Y-%m-%d %H:%M:%S UTC})" if estado.ultima_observacion_en else ""),
            f"Pico de equity vigente: {estado.pico_vigente}",
        ]

    if args.comando == "resume":
        resultado, equity = resume_system(
            session, now=now, price_source=price_source, initial_capital=initial_capital
        )
        if resultado == ResultadoReanudacion.REANUDADO:
            return [f"REANUDADO. El pico de equity se reinició al equity actual: {equity} USD."]
        return [f"{resultado.value}: {_MENSAJES_REANUDACION[resultado]}"]

    raise ValueError(f"Comando desconocido: {args.comando!r}")  # pragma: no cover - argparse lo impide


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = parse_args(argv)
    settings = get_settings()
    session_factory = get_sessionmaker(get_engine(settings))
    now = datetime.now(timezone.utc)

    with session_scope(session_factory) as session:
        for linea in run_command(
            session,
            args,
            price_source=BinanceTickerPriceSource(),
            now=now,
            initial_capital=settings.paper_capital_usd,
        ):
            print(linea)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
