"""Precios de referencia para el benchmark de cada operación (Paso 9;
decisión del usuario, 2026-10-05).

El plan (sección 8.4) pide que cada `trade_result` guarde el retorno de BTC y
el del promedio del universo durante la ventana EXACTA de la operación. Esos
precios los observa el propio motor en los dos instantes que importan —al
abrir y al cerrar la posición—, con la misma fuente que ya usa el monitor de
60 s (`PriceSource`). No dependen de las velas, que se ingieren una vez al
día, y no hay aproximación por hora ni look-ahead.

Mejor esfuerzo: si el precio de algún par no está disponible, ese par
simplemente no aparece en el resultado (nunca se inventa ni se reutiliza uno
viejo, P3), y `app.domain.rules.results.calculate_trade_benchmark` deja en
`NULL` lo que no se puede calcular completo. Un fallo de precios de
referencia JAMÁS bloquea una entrada ni una salida.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from app.domain.symbols import ALLOWED_SYMBOLS
from app.execution.price_source import PriceSource, PriceUnavailable


def fetch_reference_prices(
    price_source: PriceSource,
    *,
    universe: Sequence[str] = ALLOWED_SYMBOLS,
    known: Mapping[str, Decimal] | None = None,
) -> dict[str, Decimal]:
    """Precio actual de cada símbolo del universo que se pueda obtener.

    `known` son precios ya observados en este mismo instante (por ejemplo el
    del propio símbolo operado, que el monitor acaba de pedir): se usan tal
    cual en vez de volver a pedirlos.
    """
    precios: dict[str, Decimal] = {}
    for symbol in universe:
        if known is not None and symbol in known:
            precios[symbol] = known[symbol]
            continue
        try:
            precios[symbol] = price_source.get_price(symbol)
        except PriceUnavailable:
            continue
    return precios
