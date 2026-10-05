"""Reconciliación al arrancar el motor (P5 y sección 9 del plan).

> "Reconciliación al arrancar: el motor compara su estado contra el exchange
> antes de actuar."  /  "Discrepancia en la reconciliación al arrancar:
> notificar, bloquear operación hasta resolver."

En la fase paper no existe un exchange con posiciones reales contra el cual
comparar: el estado completo ES la base de datos. Por eso la reconciliación
de v1 verifica que `posiciones` (el estado vivo) y `ordenes` (los hechos)
cuenten la MISMA historia. Una posición cuya cantidad restante no se explica
con sus órdenes ejecutadas es exactamente el tipo de "posición fantasma" o
"orden duplicada" que P5 quiere impedir. Cuando exista Binance real
(Fase 6), aquí se agregará la comparación contra el exchange.

`find_discrepancies` es PURA (recibe filas simples, devuelve discrepancias):
se prueba sin base de datos. `reconcile` solo carga los datos y la llama.
No corrige nada: si algo no cuadra, el monitor se detiene y alguien lo mira
(el plan: "si una situación no está cubierta por una regla explícita, la
decide un humano").

Verifica, para cada posición:

1. tiene exactamente UNA orden de entrada ejecutada, y coincide con la
   cantidad y el precio de entrada de la posición;
2. `cantidad restante = cantidad inicial - suma de ventas ejecutadas`;
3. `take_profit_hit` coincide con que exista una venta `TAKE_PROFIT_PARTIAL`
   ejecutada;
4. sus órdenes son del mismo símbolo;
5. si está abierta, su símbolo sigue en la lista autorizada (si no, no habría
   forma de obtener su precio);

y que ninguna orden ejecutada quede sin posición.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from sqlalchemy.orm import Session

from app.domain.enums import EstadoOrden, EstadoPosicion, IntencionOrden, LadoOrden
from app.domain.symbols import ALLOWED_SYMBOLS
from app.repositories.orders import list_orders
from app.repositories.positions import list_posiciones


class CodigoDiscrepancia(str, Enum):
    SIN_ORDEN_DE_ENTRADA = "SIN_ORDEN_DE_ENTRADA"
    ENTRADAS_MULTIPLES = "ENTRADAS_MULTIPLES"
    ENTRADA_NO_COINCIDE = "ENTRADA_NO_COINCIDE"
    CANTIDAD_RESTANTE_NO_COINCIDE = "CANTIDAD_RESTANTE_NO_COINCIDE"
    TP_PARCIAL_NO_COINCIDE = "TP_PARCIAL_NO_COINCIDE"
    ORDEN_DE_OTRO_SIMBOLO = "ORDEN_DE_OTRO_SIMBOLO"
    SIMBOLO_NO_PERMITIDO = "SIMBOLO_NO_PERMITIDO"
    ORDEN_EJECUTADA_SIN_POSICION = "ORDEN_EJECUTADA_SIN_POSICION"


@dataclass(frozen=True)
class Discrepancia:
    codigo: CodigoDiscrepancia
    detalle: str
    position_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PosicionParaConciliar:
    id: uuid.UUID
    symbol: str
    status: EstadoPosicion
    entry_price: Decimal
    initial_quantity: Decimal
    remaining_quantity: Decimal
    take_profit_hit: bool


@dataclass(frozen=True)
class OrdenParaConciliar:
    """Una orden `FILLED`."""

    id: uuid.UUID
    position_id: uuid.UUID | None
    symbol: str
    side: LadoOrden
    intencion: IntencionOrden
    quantity: Decimal
    execution_price: Decimal


def find_discrepancies(
    posiciones: Sequence[PosicionParaConciliar],
    ordenes_ejecutadas: Sequence[OrdenParaConciliar],
    *,
    simbolos_permitidos: frozenset[str] = frozenset(ALLOWED_SYMBOLS),
) -> list[Discrepancia]:
    """Todas las discrepancias encontradas (lista vacía = todo cuadra), en un
    orden reproducible: por posición, y al final las órdenes sin posición."""
    por_posicion: dict[uuid.UUID, list[OrdenParaConciliar]] = defaultdict(list)
    sin_posicion: list[OrdenParaConciliar] = []
    for orden in ordenes_ejecutadas:
        if orden.position_id is None:
            sin_posicion.append(orden)
        else:
            por_posicion[orden.position_id].append(orden)

    conocidas = {p.id for p in posiciones}
    discrepancias: list[Discrepancia] = []

    for pos in posiciones:
        propias = por_posicion.get(pos.id, [])
        entradas = [o for o in propias if o.intencion == IntencionOrden.ENTRY]
        ventas = [o for o in propias if o.side == LadoOrden.SELL]

        if not entradas:
            discrepancias.append(
                Discrepancia(
                    CodigoDiscrepancia.SIN_ORDEN_DE_ENTRADA,
                    f"{pos.symbol}: la posición no tiene una orden de entrada ejecutada",
                    pos.id,
                )
            )
        elif len(entradas) > 1:
            discrepancias.append(
                Discrepancia(
                    CodigoDiscrepancia.ENTRADAS_MULTIPLES,
                    f"{pos.symbol}: tiene {len(entradas)} órdenes de entrada ejecutadas",
                    pos.id,
                )
            )
        else:
            entrada = entradas[0]
            if entrada.quantity != pos.initial_quantity or entrada.execution_price != pos.entry_price:
                discrepancias.append(
                    Discrepancia(
                        CodigoDiscrepancia.ENTRADA_NO_COINCIDE,
                        f"{pos.symbol}: la orden de entrada ({entrada.quantity} a {entrada.execution_price}) no "
                        f"coincide con la posición ({pos.initial_quantity} a {pos.entry_price})",
                        pos.id,
                    )
                )

        for orden in propias:
            if orden.symbol != pos.symbol:
                discrepancias.append(
                    Discrepancia(
                        CodigoDiscrepancia.ORDEN_DE_OTRO_SIMBOLO,
                        f"La orden {orden.id} es de {orden.symbol} pero su posición es de {pos.symbol}",
                        pos.id,
                    )
                )

        vendido = sum((o.quantity for o in ventas), Decimal(0))
        esperado = pos.initial_quantity - vendido
        if esperado != pos.remaining_quantity:
            discrepancias.append(
                Discrepancia(
                    CodigoDiscrepancia.CANTIDAD_RESTANTE_NO_COINCIDE,
                    f"{pos.symbol}: cantidad restante {pos.remaining_quantity} pero las órdenes dicen {esperado} "
                    f"(inicial {pos.initial_quantity} - vendido {vendido})",
                    pos.id,
                )
            )

        hay_tp = any(o.intencion == IntencionOrden.TAKE_PROFIT_PARTIAL for o in ventas)
        if hay_tp != pos.take_profit_hit:
            discrepancias.append(
                Discrepancia(
                    CodigoDiscrepancia.TP_PARCIAL_NO_COINCIDE,
                    f"{pos.symbol}: take_profit_hit={pos.take_profit_hit} pero "
                    f"{'existe' if hay_tp else 'no existe'} una venta de TP parcial ejecutada",
                    pos.id,
                )
            )

        if pos.status == EstadoPosicion.OPEN and pos.symbol not in simbolos_permitidos:
            discrepancias.append(
                Discrepancia(
                    CodigoDiscrepancia.SIMBOLO_NO_PERMITIDO,
                    f"{pos.symbol}: posición abierta de un símbolo que ya no está en la lista autorizada",
                    pos.id,
                )
            )

    for orden in sin_posicion:
        discrepancias.append(
            Discrepancia(
                CodigoDiscrepancia.ORDEN_EJECUTADA_SIN_POSICION,
                f"La orden {orden.id} ({orden.symbol}, {orden.intencion.value}) está ejecutada pero sin posición",
            )
        )
    for position_id, ordenes in por_posicion.items():
        if position_id not in conocidas:  # pragma: no cover - la clave foránea lo impide
            for orden in ordenes:
                discrepancias.append(
                    Discrepancia(
                        CodigoDiscrepancia.ORDEN_EJECUTADA_SIN_POSICION,
                        f"La orden {orden.id} apunta a la posición inexistente {position_id}",
                        position_id,
                    )
                )

    return discrepancias


def reconcile(session: Session) -> list[Discrepancia]:
    """Carga el estado y devuelve sus discrepancias (vacía = todo cuadra)."""
    posiciones = [
        PosicionParaConciliar(
            id=p.id,
            symbol=p.symbol,
            status=p.status,
            entry_price=p.entry_price,
            initial_quantity=p.initial_quantity,
            remaining_quantity=p.remaining_quantity,
            take_profit_hit=p.take_profit_hit,
        )
        for p in list_posiciones(session)
    ]
    ordenes = [
        OrdenParaConciliar(
            id=o.id,
            position_id=o.position_id,
            symbol=o.symbol,
            side=o.side,
            intencion=o.intencion,
            quantity=o.quantity,
            execution_price=o.execution_price,
        )
        for o in list_orders(session, estados=[EstadoOrden.FILLED])
    ]
    return find_discrepancies(posiciones, ordenes)
