"""Estudio offline de la hipótesis H1 (soporte + RSI 4h bajo -> recuperación).

Protocolo pre-registrado: `claude/protocolo-h1-soporte.md` (2026-10-08). Las
constantes y reglas viven en `app/domain/rules/support.py` y en este módulo y
NO se cambian tras ver resultados. No modifica el score, ningún prompt ni
ninguna regla viva; es descriptivo/inferencial y reproducible (semilla fija).

Sin look-ahead: la decisión del día D (00:00 UTC) usa solo velas diarias con
apertura < D, velas de 4h que cerraron a las 00:00 de D o antes, y entra al
cierre de la vela de 1h que cierra a las 00:00 de D; el desenlace mira solo
velas de 1h POSTERIORES (`barrier_study.evaluate_entry`).

Funciones puras sobre `Decimal`: sin base de datos, red ni LLM.
"""

from __future__ import annotations

import random
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum

from app.data.barrier_study import ParBarreras, ResultadoBarrera, break_even_target_rate, evaluate_entry
from app.data.exchange_client import Candle
from app.data.indicator_calculator import wilder_rsi
from app.domain.rules.risk import STOP_LOSS_PCT, TAKE_PROFIT_PCT
from app.domain.rules.support import (
    UMBRAL_RSI_4H,
    VELAS_4H_MINIMAS,
    VELAS_DIARIAS_MINIMAS,
    esta_cerca,
    indices_minimos_oscilacion,
    soporte_fractal_desde_indices,
    soporte_minimo_90d,
)

PAR_BARRERAS = ParBarreras(stop_pct=STOP_LOSS_PCT, target_pct=TAKE_PROFIT_PCT)
FRACCION_EXPLORACION = Decimal("0.70")
SEMILLA_BOOTSTRAP = 20261008
ITERACIONES_BOOTSTRAP = 2000
PERCENTIL_BAJO = 1.25
PERCENTIL_ALTO = 98.75
"""Intervalo de 97,5 % (Bonferroni por comparar dos definiciones)."""
EPISODIOS_RESUELTOS_MINIMOS = 30

_UN_DIA = timedelta(days=1)
_CUATRO_HORAS = timedelta(hours=4)
_UNA_HORA = timedelta(hours=1)

BASE = "base"
RSI_SOLO = "control_B_rsi_solo"
S1_CERCA = "control_A_s1_cerca"
S2_CERCA = "control_A_s2_cerca"
S1_COND = "S1_fractal"
S2_COND = "S2_minimo_90d"
DEFINICIONES = (S1_COND, S2_COND)
GRUPOS = (BASE, RSI_SOLO, S1_CERCA, S2_CERCA, S1_COND, S2_COND)


class Particion(str, Enum):
    EXPLORACION = "exploracion"
    CONFIRMACION = "confirmacion"


class Veredicto(str, Enum):
    NO_CONCLUYENTE = "NO_CONCLUYENTE_MUESTRA_INSUFICIENTE"
    NO_RESPALDADA = "H1_NO_RESPALDADA"
    RESPALDADA = "H1_RESPALDADA"


@dataclass(frozen=True)
class Observacion:
    symbol: str
    fecha: date
    resultado: ResultadoBarrera
    banderas: frozenset[str]
    """Grupos a los que pertenece este día (siempre incluye BASE)."""
    inicio_de_racha: frozenset[str]
    """De esos grupos, aquellos en los que el día es el primero de una racha
    de días consecutivos (= episodio)."""


def _decision_a_medianoche(dia: date) -> datetime:
    return datetime(dia.year, dia.month, dia.day, tzinfo=timezone.utc)


def observaciones_de_simbolo(
    symbol: str, diarias: list[Candle], velas_4h: list[Candle], velas_1h: list[Candle]
) -> list[Observacion]:
    """Una observación por día elegible de `symbol`. Las listas deben venir
    ordenadas por `open_time` y contener solo velas CERRADAS."""
    if not diarias or not velas_4h or not velas_1h:
        return []

    lows = [c["low"] for c in diarias]
    cierres_d = [c["close"] for c in diarias]
    abiertas_d = [c["open_time"] for c in diarias]
    minimos_osc = indices_minimos_oscilacion(lows)
    rsi_4h = wilder_rsi([c["close"] for c in velas_4h])
    abiertas_4h = [c["open_time"] for c in velas_4h]
    indice_1h = {c["open_time"]: i for i, c in enumerate(velas_1h)}

    observaciones: list[Observacion] = []
    previas: dict[str, tuple[date, bool]] = {}  # grupo -> (fecha anterior observada, estaba activo)

    dia = abiertas_d[0].date() + _UN_DIA
    ultimo_dia = abiertas_d[-1].date() + _UN_DIA
    while dia <= ultimo_dia:
        decision = _decision_a_medianoche(dia)
        n_d = bisect_left(abiertas_d, decision)  # velas diarias con apertura < decision
        n_4h = bisect_right(abiertas_4h, decision - _CUATRO_HORAS)  # velas de 4h ya cerradas a las 00:00
        i_1h = indice_1h.get(decision - _UNA_HORA)
        if (
            n_d >= VELAS_DIARIAS_MINIMAS
            and n_4h >= VELAS_4H_MINIMAS
            and i_1h is not None
            and abiertas_d[n_d - 1] == decision - _UN_DIA
            and abiertas_4h[n_4h - 1] == decision - _CUATRO_HORAS
            and i_1h + 1 < len(velas_1h)
        ):
            precio = cierres_d[n_d - 1]
            rsi = rsi_4h[n_4h - 1]
            rsi_bajo = rsi is not None and rsi < UMBRAL_RSI_4H
            s1 = soporte_fractal_desde_indices(lows[:n_d], minimos_osc, n_d - 1, precio)
            s2 = soporte_minimo_90d(lows[:n_d])
            cerca1 = esta_cerca(precio, s1)
            cerca2 = esta_cerca(precio, s2)
            activos = {BASE}
            if rsi_bajo:
                activos.add(RSI_SOLO)
            if cerca1:
                activos.add(S1_CERCA)
            if cerca2:
                activos.add(S2_CERCA)
            if cerca1 and rsi_bajo:
                activos.add(S1_COND)
            if cerca2 and rsi_bajo:
                activos.add(S2_COND)

            inicios = set()
            for grupo in activos:
                anterior = previas.get(grupo)
                if anterior is None or anterior[0] != dia - _UN_DIA or not anterior[1]:
                    inicios.add(grupo)
            for grupo in GRUPOS:
                previas[grupo] = (dia, grupo in activos)

            evaluacion = evaluate_entry(velas_1h, i_1h, PAR_BARRERAS)
            observaciones.append(
                Observacion(symbol, dia, evaluacion.resultado, frozenset(activos), frozenset(inicios))
            )
        dia += _UN_DIA
    return observaciones


def fecha_de_corte(observaciones: list[Observacion]) -> date | None:
    """Corte 70/30 sobre el RANGO de fechas elegibles (no sobre el número de
    observaciones). Exploración: fecha < corte. Confirmación: fecha >= corte."""
    if not observaciones:
        return None
    primera = min(o.fecha for o in observaciones)
    ultima = max(o.fecha for o in observaciones)
    dias = (ultima - primera).days
    return primera + timedelta(days=int(Decimal(dias) * FRACCION_EXPLORACION))


def _particion_de(fecha: date, corte: date) -> Particion:
    return Particion.EXPLORACION if fecha < corte else Particion.CONFIRMACION


@dataclass(frozen=True)
class Conteo:
    objetivo: int = 0
    stop: int = 0
    ambiguas: int = 0
    sin_resolver: int = 0

    @property
    def total(self) -> int:
        return self.objetivo + self.stop + self.ambiguas + self.sin_resolver

    @property
    def resueltas(self) -> int:
        return self.objetivo + self.stop

    @property
    def tasa(self) -> Decimal | None:
        return None if self.resueltas == 0 else Decimal(self.objetivo) / Decimal(self.resueltas)


def contar(observaciones: list[Observacion]) -> Conteo:
    def n(r: ResultadoBarrera) -> int:
        return sum(1 for o in observaciones if o.resultado == r)

    return Conteo(
        n(ResultadoBarrera.TARGET_PRIMERO),
        n(ResultadoBarrera.STOP_PRIMERO),
        n(ResultadoBarrera.AMBIGUA),
        n(ResultadoBarrera.SIN_RESOLVER),
    )


def seleccionar(
    observaciones: list[Observacion], grupo: str, particion: Particion, corte: date, *, solo_episodios: bool
) -> list[Observacion]:
    return [
        o
        for o in observaciones
        if grupo in o.banderas
        and _particion_de(o.fecha, corte) == particion
        and (not solo_episodios or grupo in o.inicio_de_racha)
    ]


def _percentil(valores_ordenados: list[float], p: float) -> float:
    if not valores_ordenados:
        raise ValueError("sin valores")
    posicion = (len(valores_ordenados) - 1) * p / 100
    bajo = int(posicion)
    alto = min(bajo + 1, len(valores_ordenados) - 1)
    return valores_ordenados[bajo] + (valores_ordenados[alto] - valores_ordenados[bajo]) * (posicion - bajo)


@dataclass(frozen=True)
class IntervaloDiferencia:
    diferencia: Decimal | None
    bajo: Decimal | None
    alto: Decimal | None
    remuestreos_validos: int


def diferencia_con_bootstrap(
    condicionales: list[Observacion],
    base: list[Observacion],
    *,
    iteraciones: int = ITERACIONES_BOOTSTRAP,
    semilla: int = SEMILLA_BOOTSTRAP,
) -> IntervaloDiferencia:
    """tasa(condicional) - tasa(base) y su intervalo percentil de 97,5 %,
    remuestreando FECHAS (clúster) con reemplazo: las monedas se mueven en
    bloque, así que el día es la unidad independiente más defendible."""
    c_cond, c_base = contar(condicionales), contar(base)
    if c_cond.tasa is None or c_base.tasa is None:
        return IntervaloDiferencia(None, None, None, 0)
    diferencia = c_cond.tasa - c_base.tasa

    fechas = sorted({o.fecha for o in base} | {o.fecha for o in condicionales})
    por_fecha_cond = {f: [0, 0] for f in fechas}
    por_fecha_base = {f: [0, 0] for f in fechas}
    for destino, origen in ((por_fecha_cond, condicionales), (por_fecha_base, base)):
        for o in origen:
            if o.resultado == ResultadoBarrera.TARGET_PRIMERO:
                destino[o.fecha][0] += 1
            elif o.resultado == ResultadoBarrera.STOP_PRIMERO:
                destino[o.fecha][1] += 1
    cond = [tuple(por_fecha_cond[f]) for f in fechas]
    bas = [tuple(por_fecha_base[f]) for f in fechas]

    rng = random.Random(semilla)
    k = len(fechas)
    muestras: list[float] = []
    for _ in range(iteraciones):
        elegidas = [rng.randrange(k) for _ in range(k)]
        ct = sum(cond[i][0] for i in elegidas)
        cs = sum(cond[i][1] for i in elegidas)
        bt = sum(bas[i][0] for i in elegidas)
        bs = sum(bas[i][1] for i in elegidas)
        if ct + cs == 0 or bt + bs == 0:
            continue
        muestras.append(ct / (ct + cs) - bt / (bt + bs))
    if not muestras:
        return IntervaloDiferencia(diferencia, None, None, 0)
    muestras.sort()
    return IntervaloDiferencia(
        diferencia,
        Decimal(str(_percentil(muestras, PERCENTIL_BAJO))),
        Decimal(str(_percentil(muestras, PERCENTIL_ALTO))),
        len(muestras),
    )


@dataclass(frozen=True)
class Comparacion:
    grupo: str
    particion: Particion
    episodios: Conteo
    senales: Conteo
    base: Conteo
    intervalo: IntervaloDiferencia
    equilibrio: Decimal

    @property
    def cumple_intervalo(self) -> bool:
        return self.intervalo.bajo is not None and self.intervalo.bajo > 0

    @property
    def supera_equilibrio(self) -> bool:
        return self.episodios.tasa is not None and self.episodios.tasa > self.equilibrio

    @property
    def muestra_suficiente(self) -> bool:
        return self.episodios.resueltas >= EPISODIOS_RESUELTOS_MINIMOS

    @property
    def pasa(self) -> bool:
        return self.muestra_suficiente and self.cumple_intervalo and self.supera_equilibrio


def comparar(observaciones: list[Observacion], grupo: str, particion: Particion, corte: date) -> Comparacion:
    episodios = seleccionar(observaciones, grupo, particion, corte, solo_episodios=True)
    senales = seleccionar(observaciones, grupo, particion, corte, solo_episodios=False)
    base = seleccionar(observaciones, BASE, particion, corte, solo_episodios=False)
    return Comparacion(
        grupo=grupo,
        particion=particion,
        episodios=contar(episodios),
        senales=contar(senales),
        base=contar(base),
        intervalo=diferencia_con_bootstrap(episodios, base),
        equilibrio=break_even_target_rate(PAR_BARRERAS),
    )


@dataclass(frozen=True)
class Informe:
    corte: date
    comparaciones: dict[tuple[str, Particion], Comparacion]
    veredicto: Veredicto
    ganadora: str | None


def decidir(comparaciones: dict[tuple[str, Particion], Comparacion]) -> tuple[Veredicto, str | None]:
    """Regla fija del protocolo. Se evalúa en CONFIRMACIÓN; si pasan ambas
    definiciones gana la de mayor diferencia en EXPLORACIÓN."""
    confirmacion = {d: comparaciones[(d, Particion.CONFIRMACION)] for d in DEFINICIONES}
    if not any(c.muestra_suficiente for c in confirmacion.values()):
        return Veredicto.NO_CONCLUYENTE, None
    pasan = [d for d, c in confirmacion.items() if c.pasa]
    if not pasan:
        return Veredicto.NO_RESPALDADA, None

    def diferencia_exploracion(d: str) -> Decimal:
        valor = comparaciones[(d, Particion.EXPLORACION)].intervalo.diferencia
        return valor if valor is not None else Decimal("-Infinity")

    return Veredicto.RESPALDADA, max(pasan, key=diferencia_exploracion)


def analizar(observaciones: list[Observacion]) -> Informe | None:
    corte = fecha_de_corte(observaciones)
    if corte is None:
        return None
    comparaciones = {
        (grupo, particion): comparar(observaciones, grupo, particion, corte)
        for grupo in GRUPOS
        if grupo != BASE
        for particion in Particion
    }
    veredicto, ganadora = decidir(comparaciones)
    return Informe(corte, comparaciones, veredicto, ganadora)
