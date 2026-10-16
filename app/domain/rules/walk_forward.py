"""Reglas puras de la validación WALK-FORWARD del score cuantitativo (Fase C,
2026-10-11).

Pregunta que responde (plan 8.7, preguntas 1 y 4; Fase 5): ¿las observaciones
con score ALTO rinden mejor que las de score BAJO, y se mantiene FUERA de la
muestra? Todo es determinista y sin IA (P1); no lee la base ni la red.

## Decisiones del usuario (2026-10-11)

- Ventanas por defecto: entrenamiento 12 meses, prueba 3, paso 3 (parámetros de
  análisis editables, NO reglas de riesgo).
- Solo el score DETERMINISTA (`quantitative_score`): la IA queda fuera del
  backtest (costo cero, sin look-ahead, reproducible). El resultado valida esa
  parte del score y nada más.

## Decisiones derivadas (a aprobar; ver `claude/decisiones-walk-forward.md`)

- Universo: TODA combinación símbolo-día elegible, sin filtro por umbral. Para
  preguntar "¿alto > bajo?" hace falta el rango completo de scores; filtrar por
  un umbral recortaría justo lo que se quiere comparar.
- Resultado de cada observación: retorno neto de simular las reglas de salida del
  motor paper (stop -8 %, cierre 50 % en +12 %, trailing 6 %, comisión 0,1 % por
  lado) desde el cierre de la vela de 1h de las 00:00 UTC. Expectancy = media.
- Método estadístico: EL DEL PLAN (terciles + bootstrap 95 %, 1.000 iteraciones,
  semilla fija), con el mismo núcleo que la validación de operaciones reales
  (`bootstrap_diferencia_de_terciles`).

## Qué hace (y qué NO hace) el entrenamiento

La fórmula del score tiene constantes FIJAS: no hay nada que ajustar en el
entrenamiento. Por eso el tramo de entrenamiento no calibra nada; se reporta la
misma medición DENTRO de la muestra para compararla con la de FUERA (pregunta 4
del plan: ¿se mantiene la relación fuera de la muestra?). Un score que
discrimina en entrenamiento y no en prueba es la señal de alerta clásica.

## Sin look-ahead

- Una observación del día D usa solo velas diarias cerradas antes de D (lo
  garantiza quien las construye, `app.data.backtest_study`).
- Purga: una observación de ENTRENAMIENTO cuyo desenlace (cierre de la
  posición) cae en o después del inicio de la prueba se excluye, para que
  información de la ventana de prueba no se filtre al entrenamiento.
- Las observaciones sin desenlace conocido (datos terminaron antes de cerrar la
  posición) se excluyen y se CUENTAN; nunca se rellenan.

## Limitaciones que el informe declara siempre

Las observaciones de monedas distintas el mismo día no son independientes
(el mercado cripto se mueve en bloque) y las de una misma moneda en días
seguidos se solapan en el tiempo: el bootstrap las trata como independientes,
así que el intervalo es OPTIMISTA (más estrecho de lo que debería).
"""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum

from app.domain.rules.results import (
    BOOTSTRAP_ITERACIONES,
    BOOTSTRAP_SEMILLA,
    MUESTRA_MINIMA_DEL_PLAN,
    MUESTRA_MINIMA_PARA_TERCILES,
    bootstrap_diferencia_de_terciles,
)

WALK_FORWARD_VERSION = "1"
"""Sube si cambia cualquier regla de este módulo (reproducibilidad)."""

ENTRENAMIENTO_MESES_DEFECTO = 12
PRUEBA_MESES_DEFECTO = 3
PASO_MESES_DEFECTO = 3

_ESCALA_RETORNO = Decimal("1E-8")
_CERO = Decimal(0)
_UN_DIA = timedelta(days=1)


@dataclass(frozen=True)
class VentanasWalkForward:
    entrenamiento_meses: int = ENTRENAMIENTO_MESES_DEFECTO
    prueba_meses: int = PRUEBA_MESES_DEFECTO
    paso_meses: int = PASO_MESES_DEFECTO

    def __post_init__(self) -> None:
        for nombre, valor in (
            ("entrenamiento_meses", self.entrenamiento_meses),
            ("prueba_meses", self.prueba_meses),
            ("paso_meses", self.paso_meses),
        ):
            if valor < 1:
                raise ValueError(f"{nombre} debe ser >= 1 (recibido {valor}).")


def sumar_meses(dia: date, meses: int) -> date:
    """Suma meses de calendario; si el día no existe en el mes destino se usa su
    último día (31 ene + 1 mes = 28/29 feb)."""
    indice = dia.year * 12 + (dia.month - 1) + meses
    anio, mes = divmod(indice, 12)
    mes += 1
    return date(anio, mes, min(dia.day, calendar.monthrange(anio, mes)[1]))


@dataclass(frozen=True)
class Pliegue:
    """Rangos [desde, hasta) de fechas de DECISIÓN (UTC)."""

    indice: int
    entrena_desde: date
    entrena_hasta: date  # = prueba_desde
    prueba_hasta: date

    @property
    def prueba_desde(self) -> date:
        return self.entrena_hasta


def generar_pliegues(primer_dia: date, ultimo_dia: date, ventanas: VentanasWalkForward) -> list[Pliegue]:
    """Pliegues que caben ENTEROS entre `primer_dia` y `ultimo_dia` (ambos
    inclusive). Un tramo de prueba incompleto no se usa: compararía periodos de
    distinto largo."""
    pliegues: list[Pliegue] = []
    i = 0
    while True:
        entrena_desde = sumar_meses(primer_dia, i * ventanas.paso_meses)
        entrena_hasta = sumar_meses(entrena_desde, ventanas.entrenamiento_meses)
        prueba_hasta = sumar_meses(entrena_hasta, ventanas.prueba_meses)
        if prueba_hasta > ultimo_dia + _UN_DIA:
            break
        pliegues.append(Pliegue(i, entrena_desde, entrena_hasta, prueba_hasta))
        i += 1
    return pliegues


@dataclass(frozen=True)
class ObservacionBacktest:
    """Una entrada hipotética: símbolo, día de decisión, score conocido en ese
    instante y desenlace simulado."""

    symbol: str
    dia: date
    score: Decimal
    retorno_neto: Decimal | None
    """Fracción del valor de entrada (0,05 = +5 % neto); `None` = censurada."""
    cierre_en: datetime | None
    """Instante UTC del cierre total simulado; `None` = censurada."""
    factores: tuple[tuple[str, Decimal], ...] = ()
    """Sub-scores (0-100) de cada factor del score, solo para diagnóstico por factor."""
    retorno_marcado: Decimal | None = None
    """Retorno si la posición se valorara al último precio disponible. Solo para el
    diagnóstico de censura; en una observación cerrada coincide con `retorno_neto`."""
    marcado_en: datetime | None = None
    """Instante UTC de la última vela usada para `retorno_marcado`."""

    def __post_init__(self) -> None:
        if (self.retorno_neto is None) != (self.cierre_en is None):
            raise ValueError("retorno_neto y cierre_en deben venir juntos o ambos ausentes.")
        if (self.retorno_marcado is None) != (self.marcado_en is None):
            raise ValueError("retorno_marcado y marcado_en deben venir juntos o ambos ausentes.")
        for momento in (self.cierre_en, self.marcado_en):
            if momento is not None and momento.utcoffset() != timedelta(0):
                raise ValueError("Los instantes de la observación deben tener zona horaria UTC explícita.")


def hasta_fecha(observaciones: Sequence[ObservacionBacktest], fecha_exclusiva: date) -> list[ObservacionBacktest]:
    """Observaciones con día de decisión ANTERIOR a `fecha_exclusiva`. Sirve para
    evaluar una época por separado (p. ej. la que no se había visto al diseñar
    el análisis). Solo descarta observaciones posteriores; no cambia ninguna otra."""
    return [o for o in observaciones if o.dia < fecha_exclusiva]


class EstadoTramo(str, Enum):
    SIN_DATOS = "SIN_DATOS"
    """Menos de 3 observaciones con desenlace: no hay terciles."""
    PRELIMINAR = "PRELIMINAR"
    """De 3 a 29 observaciones: se calcula, pero el plan pide 30."""
    EVALUABLE = "EVALUABLE"


@dataclass(frozen=True)
class ResultadoTramo:
    n: int
    """Observaciones con desenlace conocido usadas en la comparación."""
    censuradas: int
    """Excluidas por no tener desenlace conocido."""
    purgadas: int
    """Excluidas por la purga (solo entrenamiento)."""
    estado: EstadoTramo
    tercil_size: int | None
    expectancy_alta: Decimal | None
    expectancy_baja: Decimal | None
    discriminacion: Decimal | None
    ci_inferior: Decimal | None
    ci_superior: Decimal | None
    discrimina: bool | None
    """`True` si el límite inferior del IC 95 % es > 0 (regla del plan)."""


def _cuantizar(valor: Decimal) -> Decimal:
    return valor.quantize(_ESCALA_RETORNO)


def _clave_de_orden(o: ObservacionBacktest) -> tuple:
    # Score descendente; empates por fecha y símbolo: el orden NO depende del
    # orden de entrada de la lista.
    return (-o.score, o.dia, o.symbol)


def evaluar_tramo(
    observaciones: Sequence[ObservacionBacktest],
    *,
    purgadas: int = 0,
    iterations: int = BOOTSTRAP_ITERACIONES,
    seed: int = BOOTSTRAP_SEMILLA,
) -> ResultadoTramo:
    """Mide la discriminación del score en un conjunto de observaciones
    (método del plan 8.7). Las censuradas se cuentan y se excluyen."""
    con_desenlace = [o for o in observaciones if o.retorno_neto is not None]
    censuradas = len(observaciones) - len(con_desenlace)
    n = len(con_desenlace)
    if n < MUESTRA_MINIMA_PARA_TERCILES:
        return ResultadoTramo(n, censuradas, purgadas, EstadoTramo.SIN_DATOS, None, None, None, None, None, None, None)

    ordenadas = sorted(con_desenlace, key=_clave_de_orden)
    boot = bootstrap_diferencia_de_terciles(
        [o.retorno_neto for o in ordenadas],  # type: ignore[misc]  # filtradas arriba
        iterations=iterations,
        seed=seed,
    )
    return ResultadoTramo(
        n=n,
        censuradas=censuradas,
        purgadas=purgadas,
        estado=EstadoTramo.EVALUABLE if n >= MUESTRA_MINIMA_DEL_PLAN else EstadoTramo.PRELIMINAR,
        tercil_size=boot.k,
        expectancy_alta=_cuantizar(boot.alta),
        expectancy_baja=_cuantizar(boot.baja),
        discriminacion=_cuantizar(boot.alta - boot.baja),
        ci_inferior=_cuantizar(boot.ci_lower),
        ci_superior=_cuantizar(boot.ci_upper),
        discrimina=boot.ci_lower > _CERO,
    )


def _inicio_utc(dia: date) -> datetime:
    return datetime(dia.year, dia.month, dia.day, tzinfo=timezone.utc)


@dataclass(frozen=True)
class ResultadoPliegue:
    pliegue: Pliegue
    entrenamiento: ResultadoTramo
    prueba: ResultadoTramo


class Veredicto(str, Enum):
    DISCRIMINA = "DISCRIMINA"
    """Muestra evaluable (>= 30) y límite inferior del IC 95 % > 0 FUERA de la muestra."""
    NO_DISCRIMINA = "NO_DISCRIMINA"
    MUESTRA_INSUFICIENTE = "MUESTRA_INSUFICIENTE"
    SIN_PLIEGUES = "SIN_PLIEGUES"
    """El historial no alcanza para un solo pliegue completo."""


@dataclass(frozen=True)
class InformeWalkForward:
    ventanas: VentanasWalkForward
    version: str
    primer_dia: date | None
    ultimo_dia: date | None
    pliegues: tuple[ResultadoPliegue, ...]
    prueba_agregada: ResultadoTramo | None
    """Todas las ventanas de PRUEBA juntas (sin duplicar una observación que
    caiga en dos). Es la medición que decide el veredicto."""
    pliegues_con_diferencia_positiva: int
    """Informativo: en cuántas pruebas la diferencia puntual fue > 0. No es
    parte de la regla del veredicto."""
    veredicto: Veredicto
    notas: tuple[str, ...] = field(default=())


def informe_walk_forward(
    observaciones: Sequence[ObservacionBacktest],
    ventanas: VentanasWalkForward | None = None,
    *,
    iterations: int = BOOTSTRAP_ITERACIONES,
    seed: int = BOOTSTRAP_SEMILLA,
    con_entrenamiento: bool = True,
) -> InformeWalkForward:
    """`con_entrenamiento=False` omite la medición dentro de muestra (más rápido,
    para diagnósticos): los tramos de entrenamiento quedan `SIN_DATOS`."""
    ventanas = ventanas or VentanasWalkForward()
    notas: list[str] = []
    if not observaciones:
        return InformeWalkForward(ventanas, WALK_FORWARD_VERSION, None, None, (), None, 0, Veredicto.SIN_PLIEGUES,
                                  ("No hay observaciones elegibles (¿falta el backfill de ~2 años?).",))

    primer_dia = min(o.dia for o in observaciones)
    ultimo_dia = max(o.dia for o in observaciones)
    pliegues = generar_pliegues(primer_dia, ultimo_dia, ventanas)

    resultados: list[ResultadoPliegue] = []
    prueba_unica: dict[tuple[str, date], ObservacionBacktest] = {}
    for p in pliegues:
        inicio_prueba = _inicio_utc(p.prueba_desde)
        entrenamiento_bruto = [o for o in observaciones if p.entrena_desde <= o.dia < p.entrena_hasta]
        # Purga: el desenlace de una observación de entrenamiento no puede llegar
        # a la ventana de prueba. Las censuradas no se purgan: ya se excluyen y cuentan.
        entrenamiento = [o for o in entrenamiento_bruto if o.cierre_en is None or o.cierre_en <= inicio_prueba]
        purgadas = len(entrenamiento_bruto) - len(entrenamiento)
        prueba = [o for o in observaciones if p.prueba_desde <= o.dia < p.prueba_hasta]
        for o in prueba:
            prueba_unica[(o.symbol, o.dia)] = o
        resultados.append(
            ResultadoPliegue(
                p,
                evaluar_tramo(entrenamiento if con_entrenamiento else [], purgadas=purgadas, iterations=iterations, seed=seed),
                evaluar_tramo(prueba, iterations=iterations, seed=seed),
            )
        )

    if not resultados:
        notas.append(
            f"El historial elegible ({primer_dia} a {ultimo_dia}) no alcanza para un pliegue completo de "
            f"{ventanas.entrenamiento_meses}+{ventanas.prueba_meses} meses."
        )
        return InformeWalkForward(ventanas, WALK_FORWARD_VERSION, primer_dia, ultimo_dia, (), None, 0,
                                  Veredicto.SIN_PLIEGUES, tuple(notas))

    agregada = evaluar_tramo(list(prueba_unica.values()), iterations=iterations, seed=seed)
    positivos = sum(1 for r in resultados if r.prueba.discriminacion is not None and r.prueba.discriminacion > 0)

    if agregada.estado != EstadoTramo.EVALUABLE:
        veredicto = Veredicto.MUESTRA_INSUFICIENTE
    elif agregada.discrimina:
        veredicto = Veredicto.DISCRIMINA
    else:
        veredicto = Veredicto.NO_DISCRIMINA

    notas.append(
        "La fórmula del score tiene constantes fijas: el entrenamiento no calibra nada; se muestra como referencia "
        "dentro de la muestra para compararla con la de fuera."
    )
    notas.append(
        "Las observaciones de monedas distintas el mismo día y las de días seguidos de una misma moneda no son "
        "independientes: el intervalo bootstrap es optimista (más estrecho de lo real)."
    )
    notas.append("Valida solo el score DETERMINISTA (indicadores); no incluye el análisis de la IA ni el contexto de eventos.")
    if ventanas.paso_meses < ventanas.prueba_meses:
        notas.append("Las ventanas de prueba se solapan; en la medición agregada cada observación cuenta una sola vez.")
    ultimo = resultados[-1].pliegue
    if ultimo.prueba_hasta <= ultimo_dia:
        notas.append(
            f"Las observaciones del {ultimo.prueba_hasta} al {ultimo_dia} no entraron en ninguna ventana de prueba completa."
        )
    return InformeWalkForward(
        ventanas, WALK_FORWARD_VERSION, primer_dia, ultimo_dia, tuple(resultados), agregada, positivos, veredicto,
        tuple(notas),
    )
