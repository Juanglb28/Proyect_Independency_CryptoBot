"""D1 — distancia del precio a la media de 200 periodos (4 h) en BTCUSDT
(protocolo `docs/protocolo-distancia-ma200-4h.md`, v1, congelado el 2026-11-17).

Funciones PURAS sobre `Decimal`: sin red, base, modelo ni LLM (P1). Reciben velas ya
leídas (por argumento) y aplican exactamente lo que el protocolo fijó de antemano. Nada
de aquí llega a una orden, una cantidad, un stop ni al score (P2): es un estudio de
solo lectura. Los parámetros de abajo son los del protocolo; cambiarlos tras ver
resultados es otro experimento (una prueba fija sus valores).

Ausencia de información futura: `dist_t`, `pct_t` y el grupo de `t` solo usan velas con
`open_time <= t` (la media termina en `t`; el percentil usa solo distancias anteriores).
El retorno posterior `r_h(t)` es el ÚNICO dato que mira hacia adelante y se usa solo
para evaluar, nunca para clasificar.
"""

from __future__ import annotations

import random
from bisect import bisect_left, bisect_right, insort
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from math import ceil

from app.domain.rules.kronos_k1 import IntervaloBootstrap
from app.domain.rules.results import BOOTSTRAP_ITERACIONES, BOOTSTRAP_SEMILLA, NIVEL_DE_CONFIANZA_PCT

# --- Parámetros del protocolo ---------------------------------------------------------------
PERIODO_MA = 200
ALFA_EMA = Decimal(2) / Decimal(PERIODO_MA + 1)
CUATRO_HORAS = timedelta(hours=4)
UNA_HORA = timedelta(hours=1)
POSICION_MINIMA = 3 * PERIODO_MA  # 600: SMA y EMA se evalúan desde la vela 600 de cada tramo
CALENTADA_PERCENTIL = 1080  # velas elegibles sin clasificar (180 días)
HORIZONTES = (6, 18, 42)  # velas de 4 h: 1, 3 y 7 días
HORIZONTE_PRIMARIO = 18
COLA_PCT = Decimal(5)
MIN_EPISODIOS = 10
VELAS_FUERA_NUEVO_EPISODIO = 6
BLOQUE_DIAS = 14
ITERACIONES = BOOTSTRAP_ITERACIONES
SEMILLA = BOOTSTRAP_SEMILLA
UMBRAL_ALTO = Decimal("0.19")
UMBRAL_BAJO = Decimal("-0.25")
VENTANA_DIAS = 365
MAX_DISCREPANCIAS = 20

_CERO = Decimal(0)
_CIEN = Decimal(100)
_SEPARACION_EPISODIO = CUATRO_HORAS * (VELAS_FUERA_NUEVO_EPISODIO + 1)


# --- Velas y agregación 1 h -> 4 h ----------------------------------------------------------


@dataclass(frozen=True)
class Vela1h:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class Vela4h:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class ConteoAgregacion:
    velas_1h: int
    bloques_completos: int
    bloques_incompletos: int


def _utc(t: datetime) -> datetime:
    if t.tzinfo is None:
        raise ValueError("las velas deben traer zona horaria (UTC)")
    return t.astimezone(timezone.utc)


def agregar_a_4h(velas_1h: Sequence[Vela1h]) -> tuple[list[Vela4h], ConteoAgregacion]:
    """Agrupa velas CERRADAS de 1 h en bloques UTC 00-04-08-12-16-20. Un bloque solo es una
    vela de 4 h si tiene sus cuatro velas de 1 h; si falta alguna, no existe (no se rellena)
    y se cuenta como incompleto."""
    bloques: dict[datetime, dict[int, Vela1h]] = defaultdict(dict)
    for v in velas_1h:
        t = _utc(v.open_time)
        if t.minute or t.second or t.microsecond:
            raise ValueError(f"vela de 1 h que no abre en punto: {t.isoformat()}")
        inicio = t.replace(hour=t.hour - t.hour % 4)
        k = t.hour % 4
        if k in bloques[inicio]:
            raise ValueError(f"vela de 1 h duplicada: {t.isoformat()}")
        bloques[inicio][k] = v
    salida: list[Vela4h] = []
    incompletos = 0
    for inicio in sorted(bloques):
        g = bloques[inicio]
        if len(g) < 4:
            incompletos += 1
            continue
        partes = [g[k] for k in range(4)]
        salida.append(
            Vela4h(
                open_time=inicio,
                open=partes[0].open,
                high=max(p.high for p in partes),
                low=min(p.low for p in partes),
                close=partes[3].close,
            )
        )
    return salida, ConteoAgregacion(len(velas_1h), len(salida), incompletos)


@dataclass(frozen=True)
class ControlAgregacion:
    """Control 1 del protocolo: la vela de 4 h agregada debe coincidir EXACTAMENTE
    (apertura, máximo, mínimo, cierre) con la de 4 h guardada, donde existan ambas."""

    comparadas: int
    coinciden: int
    discrepancias: tuple[tuple[datetime, str], ...]  # hasta MAX_DISCREPANCIAS
    solo_guardadas: int  # velas de 4 h guardadas sin equivalente agregado (bloque 1 h incompleto)

    @property
    def n_discrepancias(self) -> int:
        return self.comparadas - self.coinciden

    @property
    def valido(self) -> bool:
        return self.n_discrepancias == 0


def comparar_con_4h_guardadas(agregadas: Sequence[Vela4h], guardadas: Sequence[Vela4h]) -> ControlAgregacion:
    por_tiempo = {_utc(v.open_time): v for v in agregadas}
    comparadas = coinciden = solo_guardadas = 0
    discrepancias: list[tuple[datetime, str]] = []
    for g in guardadas:
        t = _utc(g.open_time)
        a = por_tiempo.get(t)
        if a is None:
            solo_guardadas += 1
            continue
        comparadas += 1
        malos = [c for c in ("open", "high", "low", "close") if getattr(a, c) != getattr(g, c)]
        if malos:
            if len(discrepancias) < MAX_DISCREPANCIAS:
                discrepancias.append((t, ",".join(malos)))
        else:
            coinciden += 1
    return ControlAgregacion(comparadas, coinciden, tuple(discrepancias), solo_guardadas)


# --- Tramos continuos, medias y distancia ---------------------------------------------------


def tramos_contiguos(velas: Sequence[Vela4h]) -> list[list[Vela4h]]:
    """Divide la serie (estrictamente creciente en el tiempo) en tramos sin velas faltantes."""
    tramos: list[list[Vela4h]] = []
    actual: list[Vela4h] = []
    for v in velas:
        if actual:
            paso = _utc(v.open_time) - _utc(actual[-1].open_time)
            if paso <= timedelta(0):
                raise ValueError("las velas deben estar ordenadas y sin duplicados")
            if paso != CUATRO_HORAS:
                tramos.append(actual)
                actual = []
        actual.append(v)
    if actual:
        tramos.append(actual)
    return tramos


@dataclass(frozen=True)
class ObsDistancia:
    open_time: datetime
    cierre: Decimal
    dist_sma: Decimal
    dist_ema: Decimal
    posicion: int  # 1-based dentro de su tramo continuo

    @property
    def dia(self) -> date:
        return _utc(self.open_time).date()


def serie_distancias(velas: Sequence[Vela4h]) -> list[ObsDistancia]:
    """`dist_t = cierre_t / MA_t − 1` para SMA y EMA de 200 cierres de 4 h que terminan en `t`,
    desde la vela 600 de cada tramo continuo. La EMA se siembra con la SMA de los primeros
    200 cierres del tramo y sigue recursivamente (α = 2/201)."""
    obs: list[ObsDistancia] = []
    for tramo in tramos_contiguos(velas):
        n = len(tramo)
        if n < POSICION_MINIMA:
            continue
        cierres = [v.close for v in tramo]
        suma = sum(cierres[:PERIODO_MA], _CERO)
        ema = suma / PERIODO_MA
        for i in range(PERIODO_MA, n):
            suma += cierres[i] - cierres[i - PERIODO_MA]
            sma = suma / PERIODO_MA
            ema = ema + ALFA_EMA * (cierres[i] - ema)
            posicion = i + 1
            if posicion >= POSICION_MINIMA:
                obs.append(
                    ObsDistancia(
                        open_time=_utc(tramo[i].open_time),
                        cierre=cierres[i],
                        dist_sma=cierres[i] / sma - 1,
                        dist_ema=cierres[i] / ema - 1,
                        posicion=posicion,
                    )
                )
    return obs


# --- Percentil móvil y grupos ---------------------------------------------------------------


class Grupo(str, Enum):
    COLA_BAJA = "COLA_BAJA"
    CENTRO = "CENTRO"
    COLA_ALTA = "COLA_ALTA"


def grupo_de_percentil(pct: Decimal) -> Grupo:
    if pct <= COLA_PCT:
        return Grupo.COLA_BAJA
    if pct >= _CIEN - COLA_PCT:
        return Grupo.COLA_ALTA
    return Grupo.CENTRO


def clasificar(valores: Sequence[Decimal]) -> list[tuple[Decimal | None, Grupo | None]]:
    """Percentil móvil con ventana creciente: `pct_t` = 100 × (nº de valores ANTERIORES
    estrictamente menores que `valores[t]`) / (nº de valores anteriores). Las primeras
    `CALENTADA_PERCENTIL` observaciones no se clasifican (pero sí alimentan el historial)."""
    historial: list[Decimal] = []
    salida: list[tuple[Decimal | None, Grupo | None]] = []
    for i, x in enumerate(valores):
        if i >= CALENTADA_PERCENTIL:
            pct = _CIEN * bisect_left(historial, x) / len(historial)
            salida.append((pct, grupo_de_percentil(pct)))
        else:
            salida.append((None, None))
        insort(historial, x)
    return salida


@dataclass(frozen=True)
class ObsClasificada:
    obs: ObsDistancia
    pct_sma: Decimal | None
    grupo_sma: Grupo | None
    pct_ema: Decimal | None
    grupo_ema: Grupo | None


def clasificar_serie(obs: Sequence[ObsDistancia]) -> list[ObsClasificada]:
    sma = clasificar([o.dist_sma for o in obs])
    ema = clasificar([o.dist_ema for o in obs])
    return [ObsClasificada(o, s[0], s[1], e[0], e[1]) for o, s, e in zip(obs, sma, ema)]


# --- Retornos posteriores y episodios -------------------------------------------------------


def retornos_posteriores(velas: Sequence[Vela4h], horizonte: int) -> dict[datetime, Decimal]:
    """`r_h(t) = cierre_{t+h} / cierre_t − 1`, solo si existe la vela cerrada exactamente `h`
    velas de 4 h después. Entrada = cierre de `t`, sin comisiones ni deslizamiento."""
    cierre = {_utc(v.open_time): v.close for v in velas}
    paso = CUATRO_HORAS * horizonte
    salida: dict[datetime, Decimal] = {}
    for v in velas:
        t = _utc(v.open_time)
        futuro = cierre.get(t + paso)
        if futuro is not None:
            salida[t] = futuro / v.close - 1
    return salida


@dataclass(frozen=True)
class Episodio:
    inicio: datetime
    fin: datetime
    velas: int
    extremo: Decimal  # máximo (lado alto) o mínimo (lado bajo) de la distancia dentro del episodio


def agrupar_episodios(puntos: Sequence[tuple[datetime, Decimal]], *, alto: bool) -> list[Episodio]:
    """`puntos` = (apertura, distancia) de las velas en la cola, en orden temporal. Un
    episodio nuevo empieza si entre dos velas consecutivas de la cola median
    `VELAS_FUERA_NUEVO_EPISODIO` o más velas fuera de ella."""
    episodios: list[Episodio] = []
    inicio = fin = None
    n = 0
    extremo = _CERO
    for t, d in puntos:
        if inicio is not None and t - fin < _SEPARACION_EPISODIO:
            fin = t
            n += 1
            extremo = max(extremo, d) if alto else min(extremo, d)
            continue
        if inicio is not None:
            episodios.append(Episodio(inicio, fin, n, extremo))
        inicio = fin = t
        n = 1
        extremo = d
    if inicio is not None:
        episodios.append(Episodio(inicio, fin, n, extremo))
    return episodios


# --- Parte A: descripción -------------------------------------------------------------------


@dataclass(frozen=True)
class Distribucion:
    n: int
    minimo: Decimal
    p1: Decimal
    p5: Decimal
    p25: Decimal
    p50: Decimal
    p75: Decimal
    p95: Decimal
    p99: Decimal
    maximo: Decimal


def _rango_mas_cercano(ordenados: Sequence[Decimal], p: int) -> Decimal:
    n = len(ordenados)
    return ordenados[max(-(-p * n // 100), 1) - 1]


def distribucion(valores: Sequence[Decimal]) -> Distribucion | None:
    if not valores:
        return None
    o = sorted(valores)
    q = lambda p: _rango_mas_cercano(o, p)  # noqa: E731
    return Distribucion(len(o), o[0], q(1), q(5), q(25), q(50), q(75), q(95), q(99), o[-1])


def distribucion_por_anio(obs: Sequence[ObsDistancia], campo: str) -> dict[int, Distribucion]:
    por_anio: dict[int, list[Decimal]] = defaultdict(list)
    for o in obs:
        por_anio[o.open_time.year].append(getattr(o, campo))
    return {a: distribucion(v) for a, v in sorted(por_anio.items())}  # type: ignore[misc]


@dataclass(frozen=True)
class RupturasDeUmbral:
    velas_altas: int  # dist > +19 %
    velas_bajas: int  # dist < −25 %
    velas_total: int
    episodios_altos: tuple[Episodio, ...]
    episodios_bajos: tuple[Episodio, ...]


def rupturas_de_umbral(obs: Sequence[ObsDistancia], campo: str = "dist_sma") -> RupturasDeUmbral:
    altas = [(o.open_time, getattr(o, campo)) for o in obs if getattr(o, campo) > UMBRAL_ALTO]
    bajas = [(o.open_time, getattr(o, campo)) for o in obs if getattr(o, campo) < UMBRAL_BAJO]
    return RupturasDeUmbral(
        len(altas), len(bajas), len(obs),
        tuple(agrupar_episodios(altas, alto=True)), tuple(agrupar_episodios(bajas, alto=False)),
    )


@dataclass(frozen=True)
class VentanasAnuales:
    ventanas: int
    sin_ruptura: int
    dias_con_ruptura: tuple[date, ...]

    @property
    def fraccion_sin_ruptura(self) -> Fraction | None:
        return Fraction(self.sin_ruptura, self.ventanas) if self.ventanas else None


def ventanas_anuales_sin_ruptura(obs: Sequence[ObsDistancia], campo: str = "dist_sma") -> VentanasAnuales:
    """Para cada ventana móvil de 365 días de calendario (paso de 1 día) dentro de la muestra,
    ¿contiene alguna vela fuera de [−25 %, +19 %]?"""
    if not obs:
        return VentanasAnuales(0, 0, ())
    dias_ruptura = sorted({o.dia for o in obs if getattr(o, campo) > UMBRAL_ALTO or getattr(o, campo) < UMBRAL_BAJO})
    ordinales = [d.toordinal() for d in dias_ruptura]
    primero, ultimo = obs[0].dia.toordinal(), obs[-1].dia.toordinal()
    ventanas = sin = 0
    for inicio in range(primero, ultimo - VENTANA_DIAS + 2):
        fin = inicio + VENTANA_DIAS - 1
        ventanas += 1
        if bisect_right(ordinales, fin) - bisect_left(ordinales, inicio) == 0:
            sin += 1
    return VentanasAnuales(ventanas, sin, tuple(dias_ruptura))


# --- Parte B: contrastes --------------------------------------------------------------------


class VeredictoD1(str, Enum):
    NO_CONCLUYENTE_MUESTRA_INSUFICIENTE = "NO_CONCLUYENTE_MUESTRA_INSUFICIENTE"
    SIN_EVIDENCIA = "SIN_EVIDENCIA"
    EN_LA_DIRECCION_ESPERADA = "EN_LA_DIRECCION_ESPERADA"
    EN_CONTRA = "EN_CONTRA"


@dataclass(frozen=True)
class Contraste:
    ma: str  # "SMA", "EMA" o "UMBRAL_FIJO"
    cola: Grupo
    horizonte: int
    velas_cola: int
    velas_centro: int
    dias: int
    episodios: int
    intervalo: IntervaloBootstrap | None  # diferencia de medias (cola − centro)
    veredicto: VeredictoD1
    muestra: str = "TODA"  # "TODA", "PRIMERA_MITAD" o "SEGUNDA_MITAD"


def _percentiles(medias: list[Decimal]) -> tuple[Decimal, Decimal]:
    medias.sort()
    cola = Fraction(100 - NIVEL_DE_CONFIANZA_PCT, 2 * 100)
    n = len(medias)
    return medias[ceil(n * cola) - 1], medias[ceil(n * (1 - cola)) - 1]


def bootstrap_dif_de_medias(
    dias: Sequence[tuple[Decimal, int, Decimal, int]],
    *,
    bloque: int = BLOQUE_DIAS,
    iteraciones: int = ITERACIONES,
    semilla: int = SEMILLA,
) -> IntervaloBootstrap | None:
    """Diferencia entre la media del grupo A y la del grupo B con IC95 por bootstrap de
    BLOQUES de `bloque` días de calendario consecutivos. Cada día es
    `(suma_A, n_A, suma_B, n_B)`; la estadística se recalcula con los días remuestreados.
    Una remuestra sin valores de A o de B se descarta y se vuelve a sortear (determinista
    con la semilla). `None` si algún grupo está vacío en toda la muestra."""
    n = len(dias)
    if n == 0 or sum(d[1] for d in dias) == 0 or sum(d[3] for d in dias) == 0:
        return None
    pa, na, pb, nb = [_CERO], [0], [_CERO], [0]
    for sa, ca, sb, cb in dias:
        pa.append(pa[-1] + sa)
        na.append(na[-1] + ca)
        pb.append(pb[-1] + sb)
        nb.append(nb[-1] + cb)
    b = min(bloque, n)
    rng = random.Random(semilla)
    medias: list[Decimal] = []
    intentos = 0
    while len(medias) < iteraciones and intentos < iteraciones * 20:
        intentos += 1
        sa = sb = _CERO
        ca = cb = cuenta = 0
        while cuenta < n:
            i = rng.randrange(n - b + 1)
            j = i + b
            sa += pa[j] - pa[i]
            ca += na[j] - na[i]
            sb += pb[j] - pb[i]
            cb += nb[j] - nb[i]
            cuenta += b
        if ca and cb:
            medias.append(sa / ca - sb / cb)
    if len(medias) < iteraciones:
        return None
    inf, sup = _percentiles(medias)
    estimado = pa[-1] / na[-1] - pb[-1] / nb[-1]
    return IntervaloBootstrap(n, estimado, inf, sup)


def veredicto_de(cola: Grupo, episodios: int, intervalo: IntervaloBootstrap | None) -> VeredictoD1:
    """Hipótesis de reversión a la media: tras COLA_BAJA el retorno es MAYOR que tras CENTRO;
    tras COLA_ALTA, MENOR."""
    if episodios < MIN_EPISODIOS or intervalo is None:
        return VeredictoD1.NO_CONCLUYENTE_MUESTRA_INSUFICIENTE
    if intervalo.ci_inferior <= 0 <= intervalo.ci_superior:
        return VeredictoD1.SIN_EVIDENCIA
    positivo = intervalo.ci_inferior > 0
    esperado_positivo = cola is Grupo.COLA_BAJA
    return VeredictoD1.EN_LA_DIRECCION_ESPERADA if positivo == esperado_positivo else VeredictoD1.EN_CONTRA


def contraste(
    obs: Sequence[ObsClasificada],
    retornos: Mapping[datetime, Decimal],
    *,
    ma: str,
    cola: Grupo,
    horizonte: int,
    grupo_de: Callable[[ObsClasificada], Grupo | None],
    muestra: str = "TODA",
) -> Contraste:
    """Diferencia de medias de `r_h` entre `cola` y `CENTRO`. Las observaciones sin grupo
    (calentada del percentil) o sin retorno posterior se excluyen de las medias; los
    episodios se cuentan sobre todas las velas clasificadas de la cola (aclaración 2)."""
    if cola is Grupo.CENTRO:
        raise ValueError("la cola debe ser COLA_BAJA o COLA_ALTA")
    ordenadas = sorted(obs, key=lambda c: c.obs.open_time)
    clasificadas = [(c, grupo_de(c)) for c in ordenadas]
    clasificadas = [(c, g) for c, g in clasificadas if g is not None]
    if not clasificadas:
        return Contraste(ma, cola, horizonte, 0, 0, 0, 0, None, VeredictoD1.NO_CONCLUYENTE_MUESTRA_INSUFICIENTE, muestra)
    campo = "dist_ema" if ma == "EMA" else "dist_sma"
    en_cola = [(c.obs.open_time, getattr(c.obs, campo)) for c, g in clasificadas if g is cola]
    episodios = len(agrupar_episodios(en_cola, alto=cola is Grupo.COLA_ALTA))

    por_dia: dict[date, list] = {}
    velas_cola = velas_centro = 0
    for c, g in clasificadas:
        r = retornos.get(c.obs.open_time)
        if r is None or g not in (cola, Grupo.CENTRO):
            continue
        celda = por_dia.setdefault(c.obs.dia, [_CERO, 0, _CERO, 0])
        if g is cola:
            celda[0] += r
            celda[1] += 1
            velas_cola += 1
        else:
            celda[2] += r
            celda[3] += 1
            velas_centro += 1
    if not por_dia:
        return Contraste(
            ma, cola, horizonte, 0, 0, 0, episodios, None, VeredictoD1.NO_CONCLUYENTE_MUESTRA_INSUFICIENTE, muestra
        )
    primero, ultimo = min(por_dia), max(por_dia)
    dias = []
    for k in range((ultimo - primero).days + 1):
        celda = por_dia.get(primero + timedelta(days=k))
        dias.append(tuple(celda) if celda else (_CERO, 0, _CERO, 0))
    intervalo = bootstrap_dif_de_medias(dias)
    return Contraste(
        ma, cola, horizonte, velas_cola, velas_centro, len(dias), episodios, intervalo,
        veredicto_de(cola, episodios, intervalo), muestra,
    )


def grupo_sma(c: ObsClasificada) -> Grupo | None:
    return c.grupo_sma


def grupo_ema(c: ObsClasificada) -> Grupo | None:
    return c.grupo_ema


def grupo_umbral_fijo(c: ObsClasificada) -> Grupo | None:
    """Secundario descriptivo (contaminado: los umbrales se vieron en el gráfico). Misma
    muestra clasificada que la SMA."""
    if c.grupo_sma is None:
        return None
    if c.obs.dist_sma > UMBRAL_ALTO:
        return Grupo.COLA_ALTA
    if c.obs.dist_sma < UMBRAL_BAJO:
        return Grupo.COLA_BAJA
    return Grupo.CENTRO


# --- Evaluación completa --------------------------------------------------------------------


@dataclass(frozen=True)
class ResumenCobertura:
    velas_4h: int
    tramos: int
    observaciones: int  # elegibles: posición >= 600 en su tramo
    clasificadas: int  # elegibles menos la calentada del percentil
    primera: datetime | None  # apertura de la primera vela de 4 h
    ultima: datetime | None  # apertura de la última vela de 4 h
    primera_elegible: datetime | None


def contar_cobertura(velas: Sequence[Vela4h]) -> ResumenCobertura:
    """Solo CUENTA posiciones: no calcula ninguna distancia ni retorno (modo conteo)."""
    tramos = tramos_contiguos(velas)
    elegibles = sum(max(0, len(t) - (POSICION_MINIMA - 1)) for t in tramos)
    primera_elegible = next((_utc(t[POSICION_MINIMA - 1].open_time) for t in tramos if len(t) >= POSICION_MINIMA), None)
    return ResumenCobertura(
        len(velas), len(tramos), elegibles, max(0, elegibles - CALENTADA_PERCENTIL),
        _utc(velas[0].open_time) if velas else None, _utc(velas[-1].open_time) if velas else None, primera_elegible,
    )


@dataclass(frozen=True)
class InformeD1:
    cobertura: ResumenCobertura
    n_clasificadas: int
    dist_sma: Distribucion | None
    dist_sma_por_anio: dict[int, Distribucion]
    dist_ema: Distribucion | None
    dist_ema_por_anio: dict[int, Distribucion]
    rupturas: RupturasDeUmbral
    ventanas: VentanasAnuales
    primarios: tuple[Contraste, ...]  # SMA, h=18: COLA_BAJA y COLA_ALTA (deciden)
    secundarios: tuple[Contraste, ...]  # exploratorios; no deciden
    fecha_de_corte_mitades: datetime | None


def evaluar_d1(velas: Sequence[Vela4h]) -> InformeD1:
    obs = serie_distancias(velas)
    clasificadas = clasificar_serie(obs)
    retornos = {h: retornos_posteriores(velas, h) for h in HORIZONTES}
    colas = (Grupo.COLA_BAJA, Grupo.COLA_ALTA)

    primarios = tuple(
        contraste(clasificadas, retornos[HORIZONTE_PRIMARIO], ma="SMA", cola=c, horizonte=HORIZONTE_PRIMARIO, grupo_de=grupo_sma)
        for c in colas
    )
    secundarios: list[Contraste] = []
    for h in HORIZONTES:
        if h != HORIZONTE_PRIMARIO:
            secundarios += [contraste(clasificadas, retornos[h], ma="SMA", cola=c, horizonte=h, grupo_de=grupo_sma) for c in colas]
    for h in HORIZONTES:
        secundarios += [contraste(clasificadas, retornos[h], ma="EMA", cola=c, horizonte=h, grupo_de=grupo_ema) for c in colas]
    for h in HORIZONTES:
        secundarios += [
            contraste(clasificadas, retornos[h], ma="UMBRAL_FIJO", cola=c, horizonte=h, grupo_de=grupo_umbral_fijo) for c in colas
        ]
    con_grupo = [c for c in clasificadas if c.grupo_sma is not None]
    corte = con_grupo[len(con_grupo) // 2].obs.open_time if con_grupo else None
    if corte is not None:
        primera = [c for c in clasificadas if c.obs.open_time < corte]
        segunda = [c for c in clasificadas if c.obs.open_time >= corte]
        for nombre, mitad in (("PRIMERA_MITAD", primera), ("SEGUNDA_MITAD", segunda)):
            secundarios += [
                contraste(
                    mitad, retornos[HORIZONTE_PRIMARIO], ma="SMA", cola=c, horizonte=HORIZONTE_PRIMARIO,
                    grupo_de=grupo_sma, muestra=nombre,
                )
                for c in colas
            ]
    return InformeD1(
        cobertura=contar_cobertura(velas),
        n_clasificadas=len(con_grupo),
        dist_sma=distribucion([o.dist_sma for o in obs]),
        dist_sma_por_anio=distribucion_por_anio(obs, "dist_sma"),
        dist_ema=distribucion([o.dist_ema for o in obs]),
        dist_ema_por_anio=distribucion_por_anio(obs, "dist_ema"),
        rupturas=rupturas_de_umbral(obs),
        ventanas=ventanas_anuales_sin_ruptura(obs),
        primarios=primarios,
        secundarios=tuple(secundarios),
        fecha_de_corte_mitades=corte,
    )
