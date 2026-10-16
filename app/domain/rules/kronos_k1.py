"""Evaluación de K1 (enmienda E6; protocolo `docs/protocolo-k1-kronos.md`, v1).

Funciones PURAS sobre `Decimal`: sin red, base, modelo ni torch. Reciben
observaciones ya construidas (señal de Kronos, líneas base y desenlace simulado)
y aplican exactamente lo fijado de antemano en el protocolo:

- Contraste primario ÚNICO: RankIC diario promedio (Spearman señal vs
  `retorno_neto` entre las monedas de cada fecha, fechas con >= 10 monedas),
  con IC95 por bootstrap de BLOQUES de fechas (bloque de 5, 1.000 iteraciones,
  semilla 20261005).
- Secundarios (exploratorios, no deciden): terciles de §8.7, versión E4
  (centrada por mes), permutación, correlación Kronos<->score.
- Líneas base: score v1, momentum 24 h y momentum 7 d, sobre LAS MISMAS fechas
  y monedas. «Añade información» exige superar a CADA momentum (no a «el mejor»).

Nada aquí puede llegar a una orden, una cantidad, un stop ni al score (P2).
Los parámetros de abajo son los del protocolo: cambiarlos tras ver resultados es
otro experimento (una prueba fija sus valores).
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from math import ceil

from app.domain.rules.kronos_signal import spearman
from app.domain.rules.results import (
    BOOTSTRAP_ITERACIONES,
    BOOTSTRAP_SEMILLA,
    MUESTRA_MINIMA_POR_ESTRATO,
    NIVEL_DE_CONFIANZA_PCT,
    bootstrap_diferencia_de_terciles,
)

K1_BLOQUE_FECHAS = 5
K1_MIN_FECHAS = 30
K1_MIN_MONEDAS_POR_FECHA = 10
K1_ITERACIONES = BOOTSTRAP_ITERACIONES
K1_SEMILLA = BOOTSTRAP_SEMILLA
K1_SENSIBILIDAD_DESDE = date(2025, 3, 19)

SENALES = ("kronos", "score_v1", "mom_24h", "mom_7d")
_CERO = Decimal(0)
_SEIS = Decimal("1E-6")


@dataclass(frozen=True)
class ObservacionK1:
    """Una entrada hipotética con su desenlace RESUELTO y las cuatro señales
    conocidas en el instante de decisión. Solo existen observaciones completas:
    las censuradas o con datos faltantes se descartan (y se cuentan) antes."""

    symbol: str
    dia: date
    retorno_neto: Decimal
    kronos: Decimal
    score_v1: Decimal
    mom_24h: Decimal
    mom_7d: Decimal


class VeredictoK1(str, Enum):
    NO_CONCLUYENTE = "NO_CONCLUYENTE_MUESTRA_INSUFICIENTE"
    NO_DISCRIMINA = "NO_DISCRIMINA"
    DISCRIMINA_NO_ANADE = "DISCRIMINA_NO_AÑADE"
    DISCRIMINA_Y_ANADE = "DISCRIMINA_Y_AÑADE"


@dataclass(frozen=True)
class IntervaloBootstrap:
    n: int  # fechas
    media: Decimal
    ci_inferior: Decimal
    ci_superior: Decimal


@dataclass(frozen=True)
class ResultadoTerciles:
    n: int
    k: int
    alta: Decimal
    baja: Decimal
    diferencia: Decimal
    ci_inferior: Decimal
    ci_superior: Decimal
    estratos_excluidos: int = 0  # solo la versión E4
    observaciones_excluidas: int = 0


@dataclass(frozen=True)
class InformeK1:
    n_observaciones: int
    n_fechas: int  # fechas comunes usadas (>= 10 monedas y correlación definida en las 4 señales)
    fechas_excluidas: int
    ic: dict[str, IntervaloBootstrap]
    dif_vs_mom_24h: IntervaloBootstrap | None
    dif_vs_mom_7d: IntervaloBootstrap | None
    p_permutacion: Decimal | None
    terciles: ResultadoTerciles | None
    terciles_e4: ResultadoTerciles | None
    corr_kronos_score: Decimal | None
    veredicto: VeredictoK1
    notas: tuple[str, ...] = field(default=())


def _por_dia(obs: Sequence[ObservacionK1]) -> dict[date, list[ObservacionK1]]:
    grupos: dict[date, list[ObservacionK1]] = defaultdict(list)
    for o in obs:
        grupos[o.dia].append(o)
    return {d: sorted(g, key=lambda o: o.symbol) for d, g in grupos.items()}


def ic_diarios(
    obs: Sequence[ObservacionK1], campo: str, *, minimo: int = K1_MIN_MONEDAS_POR_FECHA
) -> dict[date, Decimal]:
    """Spearman diario entre `campo` y el retorno neto. Las fechas con menos de
    `minimo` monedas, o con correlación no definida, se omiten (no se rellenan)."""
    salida: dict[date, Decimal] = {}
    for dia, grupo in _por_dia(obs).items():
        if len(grupo) < minimo:
            continue
        ic = spearman([getattr(o, campo) for o in grupo], [o.retorno_neto for o in grupo])
        if ic is not None:
            salida[dia] = ic
    return salida


def bootstrap_bloques(
    serie: Sequence[Decimal],
    *,
    bloque: int = K1_BLOQUE_FECHAS,
    iteraciones: int = K1_ITERACIONES,
    semilla: int = K1_SEMILLA,
) -> IntervaloBootstrap:
    """IC95 de la media de una serie ORDENADA POR FECHA, remuestreando bloques de
    `bloque` fechas consecutivas con reemplazo (los desenlaces de fechas vecinas
    se solapan en el tiempo). Percentiles de rango más cercano, como en §8.7."""
    n = len(serie)
    if n == 0:
        raise ValueError("No hay fechas que remuestrear.")
    if iteraciones < BOOTSTRAP_ITERACIONES:
        raise ValueError(f"el protocolo exige al menos {BOOTSTRAP_ITERACIONES} iteraciones")
    b = min(bloque, n)
    rng = random.Random(semilla)
    medias: list[Decimal] = []
    for _ in range(iteraciones):
        muestra: list[Decimal] = []
        while len(muestra) < n:
            inicio = rng.randrange(n - b + 1)
            muestra.extend(serie[inicio : inicio + b])
        medias.append(sum(muestra[:n], _CERO) / n)
    medias.sort()
    cola = Fraction(100 - NIVEL_DE_CONFIANZA_PCT, 2 * 100)
    inferior = medias[ceil(iteraciones * cola) - 1]
    superior = medias[ceil(iteraciones * (1 - cola)) - 1]
    return IntervaloBootstrap(n, sum(serie, _CERO) / n, inferior, superior)


def p_permutacion(
    obs: Sequence[ObservacionK1],
    campo: str,
    fechas: Sequence[date],
    *,
    iteraciones: int = K1_ITERACIONES,
    semilla: int = K1_SEMILLA,
) -> Decimal:
    """Fracción de barajados (de la señal entre monedas, dentro de cada fecha) cuyo
    RankIC promedio es >= al observado. Una cola, sin corrección de +1 (protocolo)."""
    grupos = _por_dia(obs)
    fechas = sorted(fechas)
    señales = {d: [getattr(o, campo) for o in grupos[d]] for d in fechas}
    retornos = {d: [o.retorno_neto for o in grupos[d]] for d in fechas}

    def media_ic(sig_por_dia: dict[date, list[Decimal]]) -> Decimal:
        total = _CERO
        for d in fechas:
            ic = spearman(sig_por_dia[d], retornos[d])
            total += ic if ic is not None else _CERO
        return total / len(fechas)

    observado = media_ic(señales)
    rng = random.Random(semilla)
    mayores = 0
    for _ in range(iteraciones):
        barajadas = {}
        for d in fechas:
            copia = list(señales[d])
            rng.shuffle(copia)
            barajadas[d] = copia
        if media_ic(barajadas) >= observado:
            mayores += 1
    return (Decimal(mayores) / iteraciones).quantize(_SEIS)


def terciles_de_senal(
    obs: Sequence[ObservacionK1],
    campo: str = "kronos",
    *,
    centrar_por_mes: bool = False,
    iteraciones: int = K1_ITERACIONES,
    semilla: int = K1_SEMILLA,
) -> ResultadoTerciles | None:
    """Método de §8.7 sobre la señal `campo`: orden descendente (desempate
    determinista), terciles `k = n // 3`, bootstrap de §8.7. Con `centrar_por_mes`
    (versión E4) el retorno se centra por mes calendario UTC de la fecha de
    decisión; los meses con una sola observación se excluyen y se cuentan."""
    excluidos_estratos = 0
    excluidas = 0
    pares: list[tuple[ObservacionK1, Decimal]]
    if centrar_por_mes:
        por_mes: dict[tuple[int, int], list[ObservacionK1]] = defaultdict(list)
        for o in obs:
            por_mes[(o.dia.year, o.dia.month)].append(o)
        pares = []
        for grupo in por_mes.values():
            if len(grupo) < MUESTRA_MINIMA_POR_ESTRATO:
                excluidos_estratos += 1
                excluidas += len(grupo)
                continue
            media = sum((o.retorno_neto for o in grupo), _CERO) / len(grupo)
            pares.extend((o, o.retorno_neto - media) for o in grupo)
    else:
        pares = [(o, o.retorno_neto) for o in obs]
    if len(pares) < 3:
        return None
    pares.sort(key=lambda p: (-getattr(p[0], campo), p[0].symbol, p[0].dia))
    boot = bootstrap_diferencia_de_terciles([v for _, v in pares], iterations=iteraciones, seed=semilla)
    return ResultadoTerciles(
        n=len(pares),
        k=boot.k,
        alta=boot.alta,
        baja=boot.baja,
        diferencia=boot.alta - boot.baja,
        ci_inferior=boot.ci_lower,
        ci_superior=boot.ci_upper,
        estratos_excluidos=excluidos_estratos,
        observaciones_excluidas=excluidas,
    )


def correlacion_kronos_score(obs: Sequence[ObservacionK1], fechas: Sequence[date]) -> Decimal | None:
    """Promedio de los Spearman diarios entre la señal de Kronos y el score v1. Si
    es alta, Kronos no aporta información distinta de la del score."""
    grupos = _por_dia(obs)
    valores = []
    for d in fechas:
        c = spearman([o.kronos for o in grupos[d]], [o.score_v1 for o in grupos[d]])
        if c is not None:
            valores.append(c)
    return (sum(valores, _CERO) / len(valores)).quantize(_SEIS) if valores else None


def _decidir(ic_kronos: IntervaloBootstrap, d24: IntervaloBootstrap, d7: IntervaloBootstrap, n_fechas: int) -> VeredictoK1:
    if n_fechas < K1_MIN_FECHAS:
        return VeredictoK1.NO_CONCLUYENTE
    if ic_kronos.ci_inferior <= 0:
        return VeredictoK1.NO_DISCRIMINA
    if d24.ci_inferior > 0 and d7.ci_inferior > 0:
        return VeredictoK1.DISCRIMINA_Y_ANADE
    return VeredictoK1.DISCRIMINA_NO_ANADE


def evaluar_k1(
    obs: Sequence[ObservacionK1], *, iteraciones: int = K1_ITERACIONES, semilla: int = K1_SEMILLA
) -> InformeK1:
    """Aplica el protocolo K1 a `obs`. Las cuatro señales se comparan sobre las
    MISMAS fechas (las que tienen >= 10 monedas y correlación definida en todas)."""
    por_senal = {s: ic_diarios(obs, s) for s in SENALES}
    comunes = sorted(set.intersection(*(set(v) for v in por_senal.values()))) if obs else []
    todas = {o.dia for o in obs}
    notas: list[str] = []
    if len(comunes) < K1_MIN_FECHAS:
        notas.append(
            f"Solo {len(comunes)} fecha(s) con >= {K1_MIN_MONEDAS_POR_FECHA} monedas resueltas; "
            f"el protocolo pide al menos {K1_MIN_FECHAS}: NO_CONCLUYENTE."
        )
    if not comunes:
        return InformeK1(len(obs), 0, len(todas), {}, None, None, None, None, None, None, VeredictoK1.NO_CONCLUYENTE, tuple(notas))

    series = {s: [por_senal[s][d] for d in comunes] for s in SENALES}
    ic = {s: bootstrap_bloques(series[s], iteraciones=iteraciones, semilla=semilla) for s in SENALES}
    dif24 = bootstrap_bloques([k - m for k, m in zip(series["kronos"], series["mom_24h"])], iteraciones=iteraciones, semilla=semilla)
    dif7 = bootstrap_bloques([k - m for k, m in zip(series["kronos"], series["mom_7d"])], iteraciones=iteraciones, semilla=semilla)
    notas.append(
        "IC por bloques de 5 fechas; con ~20 monedas muy correlacionadas la muestra efectiva es menor que n "
        "y el IC puede ser optimista. Los contrastes secundarios son exploratorios."
    )
    return InformeK1(
        n_observaciones=len(obs),
        n_fechas=len(comunes),
        fechas_excluidas=len(todas) - len(comunes),
        ic=ic,
        dif_vs_mom_24h=dif24,
        dif_vs_mom_7d=dif7,
        p_permutacion=p_permutacion(obs, "kronos", comunes, iteraciones=iteraciones, semilla=semilla),
        terciles=terciles_de_senal(obs, iteraciones=iteraciones, semilla=semilla),
        terciles_e4=terciles_de_senal(obs, centrar_por_mes=True, iteraciones=iteraciones, semilla=semilla),
        corr_kronos_score=correlacion_kronos_score(obs, comunes),
        veredicto=_decidir(ic["kronos"], dif24, dif7, len(comunes)),
        notas=tuple(notas),
    )
