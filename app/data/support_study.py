"""Estudio offline de la hipótesis H1 (soporte + RSI 4h bajo -> recuperación).

Protocolo pre-registrado: `docs/protocolo-h1-soporte.md` (v2, 2026-10-08, antes
de ver resultados). Las constantes y reglas viven en
`app/domain/rules/support.py` y en este módulo y NO se cambian tras ver
resultados. No modifica el score, ningún prompt ni
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
from app.data.exit_simulation import simular_salida
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


class EstadoDefinicion(str, Enum):
    PASA = "PASA"
    NO_PASA = "NO_PASA"
    SIN_EVALUAR = "SIN_EVALUAR_MUESTRA_INSUFICIENTE"


class Veredicto(str, Enum):
    RESPALDADA = "H1_RESPALDADA"
    NO_RESPALDADA = "H1_NO_RESPALDADA"
    NO_RESPALDADA_PARCIAL = "H1_NO_RESPALDADA_EN_LAS_EVALUADAS_OTRAS_SIN_EVALUAR"
    NO_CONCLUYENTE = "NO_CONCLUYENTE_MUESTRA_INSUFICIENTE"


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
    resuelta_en: datetime | None = None
    """Instante (cota superior: cierre de la vela) en que se tocó una barrera.
    `None` si `SIN_RESOLVER`."""
    retorno_neto: Decimal | None = None
    """Retorno neto de la salida real simulada (secundario). `None` si la
    posición no llegó a cerrarse del todo con los datos disponibles."""
    cierre_simulado_en: datetime | None = None

    @property
    def estrato(self) -> tuple[str, int, int]:
        """(moneda, año, mes) de la decisión: el 'periodo de mercado' del
        ajuste de la línea base es el mes calendario."""
        return (self.symbol, self.fecha.year, self.fecha.month)


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
            salida = simular_salida(velas_1h, i_1h)
            observaciones.append(
                Observacion(
                    symbol,
                    dia,
                    evaluacion.resultado,
                    frozenset(activos),
                    frozenset(inicios),
                    resuelta_en=(
                        None
                        if evaluacion.velas_hasta_resolver is None
                        else decision + timedelta(hours=evaluacion.velas_hasta_resolver)
                    ),
                    retorno_neto=None if salida is None else salida.retorno_neto,
                    cierre_simulado_en=(
                        None if salida is None else decision + timedelta(hours=salida.velas_hasta_cierre)
                    ),
                )
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


def _fin_exploracion(corte: date) -> datetime:
    return _decision_a_medianoche(corte)


def seleccionar(
    observaciones: list[Observacion],
    grupo: str,
    particion: Particion,
    corte: date,
    *,
    solo_episodios: bool,
    purgar_por: str = "barrera",
) -> tuple[list[Observacion], int]:
    """Observaciones del `grupo` en la `particion` y cuántas se PURGARON.

    Purga (protocolo v2): en EXPLORACIÓN se excluyen las observaciones cuyo
    desenlace ocurre en o después del corte (o que no se resolvió con los datos
    disponibles): así elegir S1 o S2 no usa información del tramo reservado para
    confirmar. `purgar_por`: "barrera" (instante en que se tocó una barrera) o
    "simulacion" (cierre total de la salida real simulada). En CONFIRMACIÓN no
    se purga nada."""
    if purgar_por not in ("barrera", "simulacion"):
        raise ValueError("purgar_por debe ser 'barrera' o 'simulacion'")
    limite = _fin_exploracion(corte)
    elegidas: list[Observacion] = []
    purgadas = 0
    for o in observaciones:
        if grupo not in o.banderas or _particion_de(o.fecha, corte) != particion:
            continue
        if solo_episodios and grupo not in o.inicio_de_racha:
            continue
        if particion == Particion.EXPLORACION:
            instante = o.resuelta_en if purgar_por == "barrera" else o.cierre_simulado_en
            if instante is None or instante >= limite:
                purgadas += 1
                continue
        elegidas.append(o)
    return elegidas, purgadas


def _percentil(valores_ordenados: list[float], p: float) -> float:
    if not valores_ordenados:
        raise ValueError("sin valores")
    posicion = (len(valores_ordenados) - 1) * p / 100
    bajo = int(posicion)
    alto = min(bajo + 1, len(valores_ordenados) - 1)
    return valores_ordenados[bajo] + (valores_ordenados[alto] - valores_ordenados[bajo]) * (posicion - bajo)


# --- Métricas, estimadores y bootstrap ------------------------------------

TASA = "tasa"
RETORNO = "retorno"


def _aporte(o: Observacion, metrica: str) -> tuple[float, int] | None:
    """(numerador, denominador) de la observación para la métrica; `None` si no
    cuenta (ambigua / sin resolver, o posición sin cerrar). Ambas métricas son
    razones: tasa = objetivos / resueltas; retorno = Σ retornos / cerradas."""
    if metrica == TASA:
        if o.resultado == ResultadoBarrera.TARGET_PRIMERO:
            return (1.0, 1)
        if o.resultado == ResultadoBarrera.STOP_PRIMERO:
            return (0.0, 1)
        return None
    if metrica == RETORNO:
        return None if o.retorno_neto is None else (float(o.retorno_neto), 1)
    raise ValueError(f"métrica desconocida: {metrica!r}")


@dataclass(frozen=True)
class Estimacion:
    """Condicional vs. línea base para una métrica. `diferencia` = condicional
    - base. Su intervalo es percentil de 97,5 % por bootstrap de FECHAS."""

    cond: Decimal | None
    base: Decimal | None
    diferencia: Decimal | None
    bajo: Decimal | None
    alto: Decimal | None
    remuestreos_validos: int
    estratos: int | None = None
    """Solo en la versión ajustada: estratos (moneda-mes) comparables usados."""

    @property
    def lim_inferior_positivo(self) -> bool:
        return self.bajo is not None and self.bajo > 0


_VACIA = Estimacion(None, None, None, None, None, 0)


def _estimar(
    mult: list[int],
    raw_cond: list[tuple[int, float, int]],
    tot_base: list[tuple[int, float, int]],
    cells_cond: dict[tuple, list[tuple[int, float, int]]],
    cells_base: dict[tuple, list[tuple[int, float, int]]],
) -> tuple[tuple[float, float] | None, tuple[float, float, int] | None]:
    """Con multiplicidades `mult` por fecha devuelve (bruta=(cond, base),
    ajustada=(cond, base, estratos)) o None donde no se puede calcular."""
    nc = dc = 0.0
    for di, num, den in raw_cond:
        m = mult[di]
        if m:
            nc += m * num
            dc += m * den
    nb = db = 0.0
    for di, num, den in tot_base:
        m = mult[di]
        if m:
            nb += m * num
            db += m * den
    bruta = (nc / dc, nb / db) if dc > 0 and db > 0 else None

    acc_cn = acc_cd = acc_b = 0.0
    usados = 0
    for estrato, celdas in cells_cond.items():
        cn = cd = 0.0
        for di, num, den in celdas:
            m = mult[di]
            if m:
                cn += m * num
                cd += m * den
        if cd <= 0:
            continue
        bn = bd = 0.0
        for di, num, den in cells_base.get(estrato, ()):
            m = mult[di]
            if m:
                bn += m * num
                bd += m * den
        if bd <= 0:
            continue
        acc_cn += cn
        acc_cd += cd
        acc_b += cd * (bn / bd)
        usados += 1
    ajustada = (acc_cn / acc_cd, acc_b / acc_cd, usados) if acc_cd > 0 else None
    return bruta, ajustada


def bootstrap_grupos(
    base: list[Observacion],
    grupos: dict[str, list[Observacion]],
    metrica: str,
    *,
    iteraciones: int = ITERACIONES_BOOTSTRAP,
    semilla: int = SEMILLA_BOOTSTRAP,
) -> dict[str, tuple[Estimacion, Estimacion]]:
    """Para cada grupo, `(bruta, ajustada)` frente a la línea base.

    - bruta: tasa/retorno del grupo menos el de TODA la línea base.
    - ajustada: la línea base se recalcula DENTRO de cada estrato (moneda-mes) y
      se promedia con peso igual a las observaciones del grupo en ese estrato;
      solo cuentan los estratos con datos en ambos lados. Así una señal que
      aparece sobre todo en caídas no se compara contra días alcistas.
    - intervalo: bootstrap por clúster de FECHA (las monedas se mueven en
      bloque), mismas multiplicidades para todos los grupos, semilla fija."""
    fechas = sorted({o.fecha for o in base} | {o.fecha for g in grupos.values() for o in g})
    indice = {f: i for i, f in enumerate(fechas)}
    n = len(fechas)

    def agregados(obs: list[Observacion]):
        por_fecha: dict[int, list[float]] = {}
        celdas: dict[tuple, dict[int, list[float]]] = {}
        for o in obs:
            a = _aporte(o, metrica)
            if a is None:
                continue
            di = indice[o.fecha]
            acum = por_fecha.setdefault(di, [0.0, 0])
            acum[0] += a[0]
            acum[1] += a[1]
            c = celdas.setdefault(o.estrato, {}).setdefault(di, [0.0, 0])
            c[0] += a[0]
            c[1] += a[1]
        total = [(di, v[0], int(v[1])) for di, v in por_fecha.items()]
        por_estrato = {k: [(di, v[0], int(v[1])) for di, v in d.items()] for k, d in celdas.items()}
        return total, por_estrato

    tot_base, cells_base = agregados(base)
    preparados = {g: agregados(obs) for g, obs in grupos.items()}

    unos = [1] * n
    puntual = {g: _estimar(unos, tot_c, tot_base, cells_c, cells_base) for g, (tot_c, cells_c) in preparados.items()}

    rng = random.Random(semilla)
    muestras_b: dict[str, list[float]] = {g: [] for g in grupos}
    muestras_a: dict[str, list[float]] = {g: [] for g in grupos}
    if n > 0:
        for _ in range(iteraciones):
            mult = [0] * n
            for _ in range(n):
                mult[rng.randrange(n)] += 1
            for g, (tot_c, cells_c) in preparados.items():
                bruta, ajustada = _estimar(mult, tot_c, tot_base, cells_c, cells_base)
                if bruta is not None:
                    muestras_b[g].append(bruta[0] - bruta[1])
                if ajustada is not None:
                    muestras_a[g].append(ajustada[0] - ajustada[1])

    def armar(punto, muestras, ajustada: bool) -> Estimacion:
        if punto is None:
            return _VACIA
        cond, base_v = Decimal(str(punto[0])), Decimal(str(punto[1]))
        estratos = punto[2] if ajustada else None
        if not muestras:
            return Estimacion(cond, base_v, cond - base_v, None, None, 0, estratos)
        muestras = sorted(muestras)
        return Estimacion(
            cond,
            base_v,
            cond - base_v,
            Decimal(str(_percentil(muestras, PERCENTIL_BAJO))),
            Decimal(str(_percentil(muestras, PERCENTIL_ALTO))),
            len(muestras),
            estratos,
        )

    return {
        g: (armar(puntual[g][0], muestras_b[g], False), armar(puntual[g][1], muestras_a[g], True)) for g in grupos
    }


@dataclass(frozen=True)
class Comparacion:
    grupo: str
    particion: Particion
    episodios: Conteo
    senales: Conteo
    base: Conteo
    purgadas_episodios: int
    purgadas_base: int
    tasa_bruta: Estimacion
    tasa_ajustada: Estimacion
    retorno_bruto: Estimacion
    """SECUNDARIO (descriptivo): retorno neto de la salida real simulada."""
    retorno_ajustado: Estimacion
    equilibrio: Decimal

    @property
    def cumple_intervalo(self) -> bool:
        """Protocolo v2: el límite inferior debe ser > 0 frente a la base BRUTA
        y frente a la base AJUSTADA por moneda y mes."""
        return self.tasa_bruta.lim_inferior_positivo and self.tasa_ajustada.lim_inferior_positivo

    @property
    def supera_equilibrio(self) -> bool:
        return self.episodios.tasa is not None and self.episodios.tasa > self.equilibrio

    @property
    def muestra_suficiente(self) -> bool:
        return self.episodios.resueltas >= EPISODIOS_RESUELTOS_MINIMOS

    @property
    def pasa(self) -> bool:
        return self.muestra_suficiente and self.cumple_intervalo and self.supera_equilibrio


def comparar_particion(
    observaciones: list[Observacion], grupos: tuple[str, ...], particion: Particion, corte: date
) -> dict[str, Comparacion]:
    base_b, purg_base_b = seleccionar(observaciones, BASE, particion, corte, solo_episodios=False)
    base_s, _ = seleccionar(observaciones, BASE, particion, corte, solo_episodios=False, purgar_por="simulacion")
    eps_b: dict[str, list[Observacion]] = {}
    eps_s: dict[str, list[Observacion]] = {}
    senales: dict[str, list[Observacion]] = {}
    purg_eps: dict[str, int] = {}
    for g in grupos:
        eps_b[g], purg_eps[g] = seleccionar(observaciones, g, particion, corte, solo_episodios=True)
        eps_s[g], _ = seleccionar(observaciones, g, particion, corte, solo_episodios=True, purgar_por="simulacion")
        senales[g], _ = seleccionar(observaciones, g, particion, corte, solo_episodios=False)
    tasas = bootstrap_grupos(base_b, eps_b, TASA)
    retornos = bootstrap_grupos(base_s, eps_s, RETORNO)
    equilibrio = break_even_target_rate(PAR_BARRERAS)
    return {
        g: Comparacion(
            grupo=g,
            particion=particion,
            episodios=contar(eps_b[g]),
            senales=contar(senales[g]),
            base=contar(base_b),
            purgadas_episodios=purg_eps[g],
            purgadas_base=purg_base_b,
            tasa_bruta=tasas[g][0],
            tasa_ajustada=tasas[g][1],
            retorno_bruto=retornos[g][0],
            retorno_ajustado=retornos[g][1],
            equilibrio=equilibrio,
        )
        for g in grupos
    }


@dataclass(frozen=True)
class Informe:
    corte: date
    comparaciones: dict[tuple[str, Particion], Comparacion]
    estados: dict[str, EstadoDefinicion]
    veredicto: Veredicto
    ganadora: str | None


def estado_de(c: Comparacion) -> EstadoDefinicion:
    if not c.muestra_suficiente:
        return EstadoDefinicion.SIN_EVALUAR
    return EstadoDefinicion.PASA if c.pasa else EstadoDefinicion.NO_PASA


def decidir(
    comparaciones: dict[tuple[str, Particion], Comparacion],
) -> tuple[Veredicto, str | None, dict[str, EstadoDefinicion]]:
    """Regla fija del protocolo v2. Cada definición se juzga POR SEPARADO en
    CONFIRMACIÓN; una definición sin muestra suficiente queda SIN EVALUAR y no
    cuenta como 'no respaldada'. Si pasan varias gana la de mayor diferencia en
    EXPLORACIÓN (la menor entre la bruta y la ajustada)."""
    estados = {d: estado_de(comparaciones[(d, Particion.CONFIRMACION)]) for d in DEFINICIONES}
    pasan = [d for d, e in estados.items() if e == EstadoDefinicion.PASA]
    if pasan:

        def diferencia_exploracion(d: str) -> Decimal:
            c = comparaciones[(d, Particion.EXPLORACION)]
            valores = [x.diferencia for x in (c.tasa_bruta, c.tasa_ajustada) if x.diferencia is not None]
            return min(valores) if valores else Decimal("-Infinity")

        return Veredicto.RESPALDADA, max(pasan, key=diferencia_exploracion), estados
    evaluadas = [e for e in estados.values() if e != EstadoDefinicion.SIN_EVALUAR]
    if not evaluadas:
        return Veredicto.NO_CONCLUYENTE, None, estados
    if len(evaluadas) == len(estados):
        return Veredicto.NO_RESPALDADA, None, estados
    return Veredicto.NO_RESPALDADA_PARCIAL, None, estados


def analizar(observaciones: list[Observacion]) -> Informe | None:
    corte = fecha_de_corte(observaciones)
    if corte is None:
        return None
    grupos = tuple(g for g in GRUPOS if g != BASE)
    comparaciones: dict[tuple[str, Particion], Comparacion] = {}
    for particion in Particion:
        for g, c in comparar_particion(observaciones, grupos, particion, corte).items():
            comparaciones[(g, particion)] = c
    veredicto, ganadora, estados = decidir(comparaciones)
    return Informe(corte, comparaciones, estados, veredicto, ganadora)
