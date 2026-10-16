"""Comandos de terminal para las entradas del motor de ejecución paper
(Paso 8). Es la interfaz manual sobre `app/execution/order_manager.py`:
Telegram y Streamlit reutilizarán el mismo servicio más adelante.

Solo mueve dinero SIMULADO (paper): no usa credenciales y el único acceso a
Binance es el ticker público de precios.

Uso:
    python -m scripts.entries open --ciclo-id <UUID>   # abre/pone en espera las entradas de un ciclo
                                                       # (estrategia + observación; el scheduler ya lo hace solo)
    python -m scripts.entries list                     # entradas pendientes y posiciones abiertas
    python -m scripts.entries approve <ORDEN_ID>       # aprueba una pendiente y la ejecuta en paper
    python -m scripts.entries reject <ORDEN_ID> [--motivo "texto"]
    python -m scripts.entries expire                   # expira las pendientes vencidas

Cada comando es una sola transacción: si algo falla, no queda nada a medias.
Las entradas pendientes vencen al terminar el día UTC del ciclo.
"""

from __future__ import annotations

import argparse
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.enums import EstadoOrden, IntencionOrden, SerieOperacion
from app.execution.order_manager import (
    ResultadoAprobacion,
    approve_pending_entry,
    expire_pending_entries,
    reject_pending_entry,
)
from app.orchestration.entry_opening import abrir_entradas_del_ciclo
from app.execution.price_source import BinanceTickerPriceSource, PriceSource
from app.logging_config import configure_logging
from app.repositories.orders import list_orders
from app.repositories.positions import list_open_posiciones

_MENSAJES_APROBACION = {
    ResultadoAprobacion.APROBADA: "Aprobada y ejecutada en paper.",
    ResultadoAprobacion.EXPIRADA: "La entrada había vencido: quedó expirada y NO se ejecutó.",
    ResultadoAprobacion.NO_PENDIENTE: "La orden ya no estaba pendiente; no se hizo nada.",
    ResultadoAprobacion.BLOQUEADA_POR_PAUSA: (
        "El sistema está pausado por drawdown: no se abren entradas nuevas. "
        "La orden sigue pendiente (puedes rechazarla)."
    ),
    ResultadoAprobacion.BLOQUEADA_POR_PARADA: (
        "Existe el archivo PARAR: no se abren entradas nuevas. "
        "La orden sigue pendiente (puedes rechazarla)."
    ),
    ResultadoAprobacion.BLOQUEADA_POR_RECONCILIACION: (
        "El estado de órdenes y posiciones no cuadra (discrepancia en la reconciliación): no se abren "
        "entradas nuevas hasta resolverlo. La orden sigue pendiente (puedes rechazarla)."
    ),
    ResultadoAprobacion.SIN_PRECIO: "No se pudo obtener un precio válido de Binance; la orden sigue pendiente.",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="comando", required=True)

    abrir = sub.add_parser("open", help="Procesa las entradas de un ciclo de análisis.")
    abrir.add_argument("--ciclo-id", type=uuid.UUID, required=True)

    sub.add_parser("list", help="Lista entradas pendientes y posiciones abiertas.")

    aprobar = sub.add_parser("approve", help="Aprueba una entrada pendiente.")
    aprobar.add_argument("orden_id", type=uuid.UUID)

    rechazar = sub.add_parser("reject", help="Rechaza una entrada pendiente.")
    rechazar.add_argument("orden_id", type=uuid.UUID)
    rechazar.add_argument("--motivo", type=str, default=None)

    sub.add_parser("expire", help="Expira las entradas pendientes vencidas.")
    return parser.parse_args(argv)


def run_command(
    session: Session, args: argparse.Namespace, *, price_source: PriceSource, now: datetime
) -> list[str]:
    """Ejecuta el comando y devuelve las líneas a imprimir. Separado de
    `main` para poder probarlo sin red ni sesión propia."""
    if args.comando == "open":
        apertura = abrir_entradas_del_ciclo(session, args.ciclo_id, now=now, price_source=price_source)
        if apertura.bloqueada_por_reconciliacion:
            lineas = [
                "BLOQUEADO: el estado de órdenes y posiciones no cuadra; no se abrió ninguna entrada. "
                "Resuélvelo a mano (la página Sistema de la interfaz muestra las discrepancias)."
            ]
            lineas.extend(f"  [{d.codigo.value}] {d.detalle}" for d in apertura.discrepancias)
            return lineas
        if not apertura.entradas:
            return [
                f"El ciclo {args.ciclo_id} no tiene propuestas NORMAL ni MAYOR_RIESGO "
                "ni candidatos del pre-filtro para observar."
            ]
        lineas = [f"Aprobaciones vencidas liberadas: {apertura.expiradas}"]
        for e in apertura.entradas:
            serie = e.serie.value
            if e.resultado == "DESCARTADA":
                lineas.append(f"  {e.symbol:10s} DESCARTADA  [{serie}]  {', '.join(e.motivos)}")
            else:
                lineas.append(
                    f"  {e.symbol:10s} {e.resultado}  [{serie}]  orden={e.orden_id}  posicion={e.posicion_id}"
                )
        return lineas

    if args.comando == "list":
        pendientes = list_orders(session, estados=[EstadoOrden.PENDING_APPROVAL], intencion=IntencionOrden.ENTRY)
        abiertas = list_open_posiciones(session)
        ids_observacion = {p.id for p in list_open_posiciones(session, serie=SerieOperacion.OBSERVACION)}
        lineas = [f"Entradas pendientes de aprobación: {len(pendientes)}"]
        for o in pendientes:
            lineas.append(f"  {o.id}  {o.symbol:10s} {o.requested_usd} USD  vence={o.expires_at.isoformat()}")
        lineas.append(f"Posiciones abiertas: {len(abiertas)}")
        for p in abiertas:
            serie = "observación" if p.id in ids_observacion else "estrategia"
            lineas.append(
                f"  {p.id}  {p.symbol:10s} [{serie}] entrada={p.entry_price} cantidad={p.remaining_quantity} "
                f"stop={p.initial_stop_price} objetivo={p.initial_target_price}"
            )
        return lineas

    if args.comando == "approve":
        resultado = approve_pending_entry(session, args.orden_id, now=now, price_source=price_source)
        return [f"{resultado.value}: {_MENSAJES_APROBACION[resultado]}"]

    if args.comando == "reject":
        if reject_pending_entry(session, args.orden_id, now=now, detail=args.motivo):
            return ["Entrada rechazada. Sus cupos quedaron libres."]
        return ["La orden ya no estaba pendiente; no se hizo nada."]

    if args.comando == "expire":
        return [f"Entradas expiradas: {expire_pending_entries(session, now)}"]

    raise ValueError(f"Comando desconocido: {args.comando!r}")  # pragma: no cover - argparse lo impide


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = parse_args(argv)
    session_factory = get_sessionmaker(get_engine(get_settings()))
    now = datetime.now(timezone.utc)

    with session_scope(session_factory) as session:
        for linea in run_command(session, args, price_source=BinanceTickerPriceSource(), now=now):
            print(linea)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
