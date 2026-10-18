"""Comparación de las dos lecturas del ORDEN INTRAVELA de la simulación de salida
(estudio de sensibilidad, 2026-10-17).

Con velas de 1h no se sabe si, dentro de una vela, el máximo o el mínimo ocurrió
primero (`app.data.exit_simulation`). Aquí se comparan, observación por observación,
los desenlaces de las dos lecturas. Todo es puro y determinista: recibe las
observaciones ya construidas (mismo símbolo y día en ambas listas) y no lee la base,
la red ni la IA.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.domain.rules.walk_forward import ObservacionBacktest

_CERO = Decimal(0)


@dataclass(frozen=True)
class ComparacionIntravela:
    n_total: int
    """Observaciones comparadas (presentes en ambas listas)."""
    n_resueltas_ambas: int
    censuradas_base: int
    censuradas_inversa: int
    media_base: Decimal | None
    media_inversa: Decimal | None
    diferencia_media: Decimal | None
    """Media de (inversa - base) sobre las resueltas en ambas."""
    n_cambian: int
    pct_cambian: Decimal
    """Fracción (0-1) de las resueltas en ambas cuyo retorno cambia."""
    n_inversa_mejor: int
    n_inversa_peor: int
    diferencia_media_si_cambian: Decimal | None
    mayor_ganancia_inversa: Decimal | None
    mayor_perdida_inversa: Decimal | None


def _clave(o: ObservacionBacktest) -> tuple[str, object]:
    return (o.symbol, o.dia)


def comparar_ordenes(
    base: Sequence[ObservacionBacktest], inversa: Sequence[ObservacionBacktest]
) -> ComparacionIntravela:
    """Compara las dos lecturas. Cada observación debe existir en ambas listas
    (misma clave símbolo-día) y con el mismo score: lo único que cambia entre
    listas es el desenlace. Si no coinciden, es un error de quien llama."""
    por_clave_inversa = {_clave(o): o for o in inversa}
    if len(por_clave_inversa) != len(inversa) or len({_clave(o) for o in base}) != len(base):
        raise ValueError("Hay observaciones duplicadas (mismo símbolo y día).")
    if {_clave(o) for o in base} != set(por_clave_inversa):
        raise ValueError("Las dos listas deben contener las mismas observaciones (símbolo y día).")

    censuradas_base = sum(1 for o in base if o.retorno_neto is None)
    censuradas_inversa = sum(1 for o in inversa if o.retorno_neto is None)

    pares: list[tuple[Decimal, Decimal]] = []
    for o in base:
        otra = por_clave_inversa[_clave(o)]
        if o.score != otra.score:
            raise ValueError(f"{o.symbol} {o.dia}: el score difiere entre lecturas; solo puede cambiar el desenlace.")
        if o.retorno_neto is not None and otra.retorno_neto is not None:
            pares.append((o.retorno_neto, otra.retorno_neto))

    n = len(pares)
    if n == 0:
        return ComparacionIntravela(
            len(base), 0, censuradas_base, censuradas_inversa, None, None, None, 0, _CERO, 0, 0, None, None, None
        )

    diferencias = [i - b for b, i in pares]
    cambian = [d for d in diferencias if d != _CERO]
    return ComparacionIntravela(
        n_total=len(base),
        n_resueltas_ambas=n,
        censuradas_base=censuradas_base,
        censuradas_inversa=censuradas_inversa,
        media_base=sum((b for b, _ in pares), _CERO) / n,
        media_inversa=sum((i for _, i in pares), _CERO) / n,
        diferencia_media=sum(diferencias, _CERO) / n,
        n_cambian=len(cambian),
        pct_cambian=Decimal(len(cambian)) / n,
        n_inversa_mejor=sum(1 for d in cambian if d > _CERO),
        n_inversa_peor=sum(1 for d in cambian if d < _CERO),
        diferencia_media_si_cambian=None if not cambian else sum(cambian, _CERO) / len(cambian),
        mayor_ganancia_inversa=None if not cambian else max(cambian),
        mayor_perdida_inversa=None if not cambian else min(cambian),
    )


@dataclass(frozen=True)
class UbicacionResuelta:
    """Dónde cae el desenlace RESUELTO con velas de 1 minuto respecto de las dos
    lecturas de 1h («máximo primero» = base, «mínimo primero» = inversa)."""

    n: int
    """Observaciones resueltas en las TRES lecturas."""
    n_sin_cambio: int
    """Las tres lecturas dan el mismo retorno."""
    n_igual_base: int
    """Resuelta == base y distinta de la inversa."""
    n_igual_inversa: int
    """Resuelta == inversa y distinta de la base."""
    n_entre: int
    """Resuelta estrictamente entre base e inversa (la «realidad intermedia»)."""
    n_fuera: int
    """Distinta de ambas y fuera del intervalo que forman (p. ej. velas con stop y
    objetivo a la vez, o con trailing en la vela del cierre parcial: la regla de 1h
    daba el mismo desenlace en las dos lecturas)."""
    media_resuelta: Decimal | None
    media_base: Decimal | None
    media_inversa: Decimal | None


def ubicar_resuelta(
    base: Sequence[ObservacionBacktest],
    inversa: Sequence[ObservacionBacktest],
    resuelta: Sequence[ObservacionBacktest],
) -> UbicacionResuelta:
    """Clasifica cada observación según dónde cae su desenlace resuelto. Las tres
    listas deben contener las mismas observaciones (símbolo y día) con el mismo score."""
    claves = [{_clave(o): o for o in lista} for lista in (base, inversa, resuelta)]
    if any(len(c) != len(lista) for c, lista in zip(claves, (base, inversa, resuelta))):
        raise ValueError("Hay observaciones duplicadas (mismo símbolo y día).")
    if not (set(claves[0]) == set(claves[1]) == set(claves[2])):
        raise ValueError("Las tres listas deben contener las mismas observaciones (símbolo y día).")

    triples: list[tuple[Decimal, Decimal, Decimal]] = []
    for k, o in claves[0].items():
        i, r = claves[1][k], claves[2][k]
        if not (o.score == i.score == r.score):
            raise ValueError(f"{o.symbol} {o.dia}: el score difiere entre lecturas; solo puede cambiar el desenlace.")
        if None not in (o.retorno_neto, i.retorno_neto, r.retorno_neto):
            triples.append((o.retorno_neto, i.retorno_neto, r.retorno_neto))  # type: ignore[arg-type]

    n = len(triples)
    if n == 0:
        return UbicacionResuelta(0, 0, 0, 0, 0, 0, None, None, None)
    sin_cambio = igual_base = igual_inversa = entre = fuera = 0
    for b, i, r in triples:
        if b == i == r:
            sin_cambio += 1
        elif r == b:
            igual_base += 1
        elif r == i:
            igual_inversa += 1
        elif min(b, i) < r < max(b, i):
            entre += 1
        else:
            fuera += 1
    return UbicacionResuelta(
        n=n,
        n_sin_cambio=sin_cambio,
        n_igual_base=igual_base,
        n_igual_inversa=igual_inversa,
        n_entre=entre,
        n_fuera=fuera,
        media_resuelta=sum((r for _, _, r in triples), _CERO) / n,
        media_base=sum((b for b, _, _ in triples), _CERO) / n,
        media_inversa=sum((i for _, i, _ in triples), _CERO) / n,
    )
