"""Formato de números y fechas para mostrar (solo presentación).

Nunca notación científica (`0E-8`) y un dato ausente se muestra como `n/d`
("no disponible"), jamás como 0: un 0 sería un dato inventado.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

ND = "n/d"


def num(valor: Decimal | int | float | None, decimales: int | None = 2, sufijo: str = "") -> str:
    """`decimales=None` conserva la precisión del dato (precios)."""
    if valor is None:
        return ND
    if isinstance(valor, Decimal):
        if decimales is None:
            texto = format(valor.normalize(), "f")
        else:
            texto = format(valor.quantize(Decimal(1).scaleb(-decimales)), "f")
        return f"{texto}{sufijo}"
    return f"{valor}{sufijo}"


def pct(valor: Decimal | None, decimales: int = 2) -> str:
    return num(valor, decimales, " %")


def usd(valor: Decimal | None, decimales: int = 2) -> str:
    return num(valor, decimales, " USD")


def precio(valor: Decimal | None) -> str:
    return num(valor, None)


def fecha(valor: datetime | None) -> str:
    return ND if valor is None else f"{valor:%Y-%m-%d %H:%M} UTC"


def duracion(valor: timedelta | None) -> str:
    """'hace 5 min' / 'hace 3 h 10 min' / 'hace 2 d 4 h'."""
    if valor is None:
        return ND
    segundos = max(int(valor.total_seconds()), 0)
    minutos, horas, dias = segundos // 60, segundos // 3600, segundos // 86400
    if minutos < 1:
        return "hace menos de 1 min"
    if horas < 1:
        return f"hace {minutos} min"
    if dias < 1:
        return f"hace {horas} h {minutos % 60} min"
    return f"hace {dias} d {horas % 24} h"


def si_no(valor: bool | None) -> str:
    return ND if valor is None else ("Sí" if valor else "No")
