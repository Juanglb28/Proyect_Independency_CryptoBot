"""Adelgazamiento de series largas para dibujarlas (solo presentación).

El monitor guarda una observación por minuto (~1.440 al día); dibujarlas todas
no aporta y enlentece la página. Esto no cambia ninguna cifra guardada ni
calculada: solo elige qué puntos se envían al gráfico.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal

from app.domain.rules.results import PuntoDeSerie


def downsample(
    puntos: Sequence[PuntoDeSerie], max_puntos: int, *, conservar: Literal["ultimo", "minimo"] = "ultimo"
) -> tuple[PuntoDeSerie, ...]:
    """Reduce `puntos` (ordenados por tiempo) a como máximo `max_puntos`.

    Siempre conserva el primer punto. El resto se agrupa en tramos de igual
    tamaño y de cada tramo se queda con el último punto (`"ultimo"`: el valor
    al cierre del tramo, para equity y precios) o con el de menor valor
    (`"minimo"`: para drawdown, de modo que la peor caída nunca se pierda).
    """
    if max_puntos < 2:
        raise ValueError("max_puntos debe ser al menos 2")
    if len(puntos) <= max_puntos:
        return tuple(puntos)
    resto = list(puntos[1:])
    tamano = math.ceil(len(resto) / (max_puntos - 1))
    elegidos = [puntos[0]]
    for inicio in range(0, len(resto), tamano):
        tramo = resto[inicio : inicio + tamano]
        elegidos.append(tramo[-1] if conservar == "ultimo" else min(tramo, key=lambda p: p.value))
    return tuple(elegidos)
