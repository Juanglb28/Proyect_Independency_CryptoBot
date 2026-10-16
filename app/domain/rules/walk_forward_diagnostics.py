"""Diagnósticos EXPLORATORIOS del backtest walk-forward (2026-10-12).

Se piden después de ver que el score cuantitativo NO discrimina fuera de muestra
(`claude/decisiones-walk-forward.md`). Responden POR QUÉ, sin cambiar el score,
sus pesos, sus límites ni ninguna regla. Reglas de uso:

- Son descriptivos. NO se usan para reajustar la fórmula: hacerlo y volver a
  presentarlo como validación está prohibido (plan 8.7). Un cambio sería una
  versión nueva de la estrategia, validada solo en datos que no se hayan visto.
- Cada intervalo es del 95 % SIN corrección por comparaciones múltiples: con
  varios diagnósticos y factores, alguno puede salir "significativo" por azar.
- Usan el MISMO motor y el mismo método estadístico que el backtest principal
  (`informe_walk_forward`); solo cambian qué se ordena o qué retorno se mide.

Los cuatro diagnósticos:

1. **Sesgo de censura**: las posiciones sin cerrar al terminar los datos se
   excluyen del veredicto. Aquí se cuentan por tercil de score y se repite la
   medición valorándolas al último precio (`retorno_marcado`).
2. **Por factor**: el tercil se arma con el sub-score de cada factor
   (tendencia, volumen, distancia al máximo de 90 d, RSI) en vez del score total.
3. **Rango dentro del día**: el score se reemplaza por su percentil entre las
   monedas observadas el MISMO día. Cada día aporta por igual a cada tercil, así
   que el efecto del régimen de mercado (días buenos vs. malos) se reparte
   parejo y lo que queda es el efecto del score. Exige un mínimo de monedas por
   día (`MINIMO_MONEDAS_POR_DIA`, parámetro de análisis nuevo, declarado).
4. **Sensibilidad de ventanas**: la misma medición con otra partición (9/3/3).

Funciones puras sobre `Decimal`: sin base de datos, red ni LLM.
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.domain.rules.results import BOOTSTRAP_ITERACIONES, BOOTSTRAP_SEMILLA, MUESTRA_MINIMA_PARA_TERCILES
from app.domain.rules.walk_forward import (
    InformeWalkForward,
    ObservacionBacktest,
    VentanasWalkForward,
    generar_pliegues,
    informe_walk_forward,
)

MINIMO_MONEDAS_POR_DIA = 10
"""Rango dentro del día: un día con menos monedas observadas no aporta (el
percentil entre pocas monedas es muy grueso). Mitad de la whitelist de 20."""

VENTANAS_SENSIBILIDAD = VentanasWalkForward(entrenamiento_meses=9, prueba_meses=3, paso_meses=3)

_CERO = Decimal(0)
_UNO = Decimal(1)


# ------------------------------------------------------------ transformaciones
def con_score_de_factor(observaciones: Sequence[ObservacionBacktest], factor: str) -> list[ObservacionBacktest]:
    """Reemplaza el score por el sub-score de `factor`. Las observaciones sin ese
    factor se omiten (no se rellenan)."""
    resultado: list[ObservacionBacktest] = []
    for o in observaciones:
        valor = dict(o.factores).get(factor)
        if valor is not None:
            resultado.append(dataclasses.replace(o, score=valor))
    return resultado


def con_rango_dentro_del_dia(
    observaciones: Sequence[ObservacionBacktest], *, minimo_monedas: int = MINIMO_MONEDAS_POR_DIA
) -> list[ObservacionBacktest]:
    """Reemplaza el score por su percentil [0, 1] entre las monedas del mismo día
    (rango medio en los empates; 0 = el más bajo, 1 = el más alto). Los días con
    menos de `minimo_monedas` observaciones se omiten."""
    por_dia: dict[date, list[ObservacionBacktest]] = defaultdict(list)
    for o in observaciones:
        por_dia[o.dia].append(o)
    resultado: list[ObservacionBacktest] = []
    for grupo in por_dia.values():
        n = len(grupo)
        if n < max(minimo_monedas, 2):
            continue
        scores = [o.score for o in grupo]
        for o in grupo:
            menores = sum(1 for s in scores if s < o.score)
            iguales = sum(1 for s in scores if s == o.score)
            rango_medio = Decimal(menores) + (Decimal(iguales) - _UNO) / 2
            resultado.append(dataclasses.replace(o, score=rango_medio / Decimal(n - 1)))
    return resultado


def con_censuradas_marcadas_a_mercado(observaciones: Sequence[ObservacionBacktest]) -> list[ObservacionBacktest]:
    """Las observaciones sin cierre se valoran al último precio disponible. Las
    que no tienen `retorno_marcado` siguen censuradas."""
    resultado: list[ObservacionBacktest] = []
    for o in observaciones:
        if o.retorno_neto is None and o.retorno_marcado is not None:
            o = dataclasses.replace(o, retorno_neto=o.retorno_marcado, cierre_en=o.marcado_en)
        resultado.append(o)
    return resultado


# --------------------------------------------------------------------- censura
@dataclass(frozen=True)
class CensuraPorTercil:
    nombre: str  # "alto" | "medio" | "bajo"
    n: int
    censuradas: int

    @property
    def fraccion(self) -> Decimal | None:
        return None if self.n == 0 else Decimal(self.censuradas) / Decimal(self.n)


def censura_por_tercil(observaciones: Sequence[ObservacionBacktest]) -> tuple[CensuraPorTercil, ...]:
    """Reparte TODAS las observaciones (con y sin cierre) en terciles de score y
    cuenta las censuradas de cada uno. Mismo orden y desempate que el backtest."""
    n = len(observaciones)
    if n < MUESTRA_MINIMA_PARA_TERCILES:
        return ()
    ordenadas = sorted(observaciones, key=lambda o: (-o.score, o.dia, o.symbol))
    k = n // 3
    grupos = (("alto", ordenadas[:k]), ("medio", ordenadas[k : n - k]), ("bajo", ordenadas[n - k :]))
    return tuple(
        CensuraPorTercil(nombre, len(g), sum(1 for o in g if o.retorno_neto is None)) for nombre, g in grupos
    )


def observaciones_de_prueba(
    observaciones: Sequence[ObservacionBacktest], ventanas: VentanasWalkForward
) -> list[ObservacionBacktest]:
    """Observaciones que caen en alguna ventana de PRUEBA (cada una una sola vez)."""
    if not observaciones:
        return []
    pliegues = generar_pliegues(min(o.dia for o in observaciones), max(o.dia for o in observaciones), ventanas)
    return [o for o in observaciones if any(p.prueba_desde <= o.dia < p.prueba_hasta for p in pliegues)]


# ------------------------------------------------------------------- conjunto
@dataclass(frozen=True)
class InformeDiagnosticos:
    ventanas: VentanasWalkForward
    censura: tuple[CensuraPorTercil, ...]
    con_censuradas_marcadas: InformeWalkForward
    por_factor: dict[str, InformeWalkForward]
    rango_dentro_del_dia: InformeWalkForward
    minimo_monedas_por_dia: int
    sensibilidad_ventanas: InformeWalkForward


FACTORES_DIAGNOSTICO = ("tendencia", "volumen", "distancia_max_90d", "rsi")


def diagnosticos(
    observaciones: Sequence[ObservacionBacktest],
    ventanas: VentanasWalkForward | None = None,
    *,
    iterations: int = BOOTSTRAP_ITERACIONES,
    seed: int = BOOTSTRAP_SEMILLA,
) -> InformeDiagnosticos:
    ventanas = ventanas or VentanasWalkForward()

    def medir(obs: Sequence[ObservacionBacktest], v: VentanasWalkForward = ventanas) -> InformeWalkForward:
        # Solo la PRUEBA (fuera de muestra): es lo que decide y es mucho más rápido.
        return informe_walk_forward(obs, v, iterations=iterations, seed=seed, con_entrenamiento=False)

    return InformeDiagnosticos(
        ventanas=ventanas,
        censura=censura_por_tercil(observaciones_de_prueba(observaciones, ventanas)),
        con_censuradas_marcadas=medir(con_censuradas_marcadas_a_mercado(observaciones)),
        por_factor={f: medir(con_score_de_factor(observaciones, f)) for f in FACTORES_DIAGNOSTICO},
        rango_dentro_del_dia=medir(con_rango_dentro_del_dia(observaciones)),
        minimo_monedas_por_dia=MINIMO_MONEDAS_POR_DIA,
        sensibilidad_ventanas=medir(observaciones, VENTANAS_SENSIBILIDAD),
    )
