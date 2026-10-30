"""Reglas de evaluación de S1, la métrica a la sombra del análisis de IA
(protocolo `docs/protocolo-metrica-sombra-ia.md`, v1).

Funciones PURAS sobre `Decimal`: sin red, base, modelo ni LLM (P1). Reciben las
decisiones que la IA ya tomó (guardadas en la base) y, solo en la fase de
evaluación, el desenlace simulado de cada candidato. Aplican exactamente lo que el
protocolo fijó de antemano:

- Contraste primario ÚNICO: diferencia pareada POR CICLO entre el retorno medio de
  los candidatos que el Consolidador incluyó (nivel 1 a 3) y los que no (nivel 0).
  Se promedia por día UTC y el IC95 sale de un bootstrap por BLOQUES de 3 días
  (1.000 iteraciones, semilla 20261005).
- Secundarios (exploratorios; no deciden): ver `evaluar_s1`.
- Veredicto: NO_CONCLUYENTE_MUESTRA_INSUFICIENTE / NO_DISCRIMINA / DISCRIMINA.

Nada aquí puede llegar a una orden, una cantidad, un stop ni al score (P2). Los
parámetros de abajo son los del protocolo: cambiarlos tras ver resultados es otro
experimento (una prueba fija sus valores).
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum, IntEnum
from fractions import Fraction
from math import ceil

from app.domain.rules.kronos_k1 import IntervaloBootstrap, bootstrap_bloques
from app.domain.rules.kronos_signal import spearman
from app.domain.rules.results import BOOTSTRAP_ITERACIONES, BOOTSTRAP_SEMILLA, NIVEL_DE_CONFIANZA_PCT

S1_BLOQUE_DIAS = 3
S1_MIN_DIAS_PAREADOS = 30
S1_MIN_CICLOS_PAREADOS = 30
S1_MIN_INCLUIDOS = 30
S1_ITERACIONES = BOOTSTRAP_ITERACIONES
S1_SEMILLA = BOOTSTRAP_SEMILLA

_CERO = Decimal(0)
_ESTADO_CANDIDATO = "CANDIDATO"
_ESTADO_SIN_DATOS = "SIN_DATOS"
_TIER_OBSERVACION = "OBSERVACION"


class NivelIA(IntEnum):
    """Lo que la IA decidió de un candidato del pre-filtro (excluyente). `NO_INCLUIDO`
    mezcla «omitido por el Consolidador» y «excluido por calidad de datos»: hoy no se
    pueden separar (limitación declarada en el protocolo)."""

    NO_INCLUIDO = 0
    NINGUNO = 1
    MAYOR_RIESGO = 2
    NORMAL = 3


_NIVEL_POR_TIER = {
    "NINGUNO": NivelIA.NINGUNO,
    "MAYOR_RIESGO": NivelIA.MAYOR_RIESGO,
    "NORMAL": NivelIA.NORMAL,
}


def nivel_de_tier(tier: str | None) -> NivelIA:
    """Nivel de una propuesta de la IA según su tier. Un tier desconocido o vacío NO se
    adivina: es una inconsistencia de datos y lanza `ValueError`."""
    try:
        return _NIVEL_POR_TIER[tier]  # type: ignore[index]
    except KeyError:
        raise ValueError(f"Tier de propuesta sin nivel definido en el protocolo S1: {tier!r}") from None


# --- Entradas (lo que lee el repositorio; sin lógica) -------------------------------------


@dataclass(frozen=True)
class CicloConConsolidador:
    ciclo_id: str
    started_at: datetime
    recomendacion: str | None  # "invertir" | "no_invertir" (de la respuesta guardada)
    prompt_version: str | None = None


@dataclass(frozen=True)
class FilaPrefiltroS1:
    ciclo_id: str
    symbol: str
    estado: str  # valor de EstadoPrefiltro
    score_final: Decimal | None


@dataclass(frozen=True)
class PropuestaS1:
    ciclo_id: str
    symbol: str
    tier: str | None  # valor de TierEntrada (None en ciclos antiguos)
    puntaje_ponderado: Decimal


# --- Unidades -------------------------------------------------------------------------------


@dataclass(frozen=True)
class UnidadBase:
    """Un símbolo en un ciclo, SIN desenlace. Existe para poder contar la muestra sin
    mirar ni una vela posterior (modo conteo del protocolo)."""

    ciclo_id: str
    started_at: datetime
    symbol: str
    es_candidato: bool
    nivel: NivelIA | None  # None si no es candidato
    puntaje_ia: Decimal | None
    score_prefiltro: Decimal | None
    recomendacion: str | None
    prompt_version: str | None = None

    @property
    def dia(self) -> date:
        return self.started_at.astimezone(timezone.utc).date()

    @property
    def incluido(self) -> bool:
        return self.nivel is not None and self.nivel != NivelIA.NO_INCLUIDO

    @property
    def no_incluido(self) -> bool:
        return self.nivel == NivelIA.NO_INCLUIDO


@dataclass(frozen=True)
class UnidadValorada(UnidadBase):
    """Una unidad con su desenlace simulado: `retorno_neto` real si la salida simulada
    ya se resolvió, o valorado a mercado al último cierre si sigue abierta."""

    retorno_neto: Decimal = _CERO
    resuelta: bool = False


def construir_unidades_base(
    ciclos: Sequence[CicloConConsolidador],
    prefiltro: Sequence[FilaPrefiltroS1],
    propuestas: Sequence[PropuestaS1],
) -> list[UnidadBase]:
    """Une, por (ciclo, símbolo), lo que el pre-filtro entregó con lo que la IA propuso.

    - Solo cuentan ciclos con llamada exitosa al Consolidador (`ciclos`).
    - Candidato (`CANDIDATO`): nivel = el mayor de sus propuestas de la IA en ese ciclo
      (puede haber una «principal» y una «menor_riesgo» del mismo símbolo); sin propuesta
      de la IA → `NO_INCLUIDO`. Las propuestas de observación (tier `OBSERVACION`) las
      crea el código y NO son decisiones de la IA: se ignoran.
    - Los demás estados salvo `SIN_DATOS` (sin puntaje, no hay entrada simulable) son el
      «resto de la lista autorizada»: `es_candidato=False`, sin nivel.
    - Una propuesta de la IA sobre un símbolo que NO es candidato ese ciclo no tiene lugar
      en el universo y se ignora (no debería existir).
    """
    por_ciclo = {c.ciclo_id: c for c in ciclos}
    candidatos = {(f.ciclo_id, f.symbol) for f in prefiltro if f.estado == _ESTADO_CANDIDATO}
    mejor: dict[tuple[str, str], tuple[NivelIA, Decimal]] = {}
    for p in propuestas:
        clave = (p.ciclo_id, p.symbol)
        # Solo cuentan propuestas de la IA sobre candidatos del pre-filtro de un ciclo con
        # Consolidador. Las de ciclos antiguos (anteriores al registro de candidatos, con tier
        # vacío) quedan fuera del universo y NO se interpretan.
        if p.ciclo_id not in por_ciclo or clave not in candidatos or p.tier == _TIER_OBSERVACION:
            continue
        nivel = nivel_de_tier(p.tier)
        actual = mejor.get(clave)
        if actual is None or (nivel, p.puntaje_ponderado) > actual:
            mejor[clave] = (nivel, p.puntaje_ponderado)

    unidades: list[UnidadBase] = []
    for f in prefiltro:
        ciclo = por_ciclo.get(f.ciclo_id)
        if ciclo is None or f.estado == _ESTADO_SIN_DATOS:
            continue
        es_candidato = (f.ciclo_id, f.symbol) in candidatos
        nivel: NivelIA | None = None
        puntaje: Decimal | None = None
        if es_candidato:
            hallado = mejor.get((f.ciclo_id, f.symbol))
            nivel, puntaje = hallado if hallado is not None else (NivelIA.NO_INCLUIDO, None)
        unidades.append(
            UnidadBase(
                ciclo_id=f.ciclo_id,
                started_at=ciclo.started_at,
                symbol=f.symbol,
                es_candidato=es_candidato,
                nivel=nivel,
                puntaje_ia=puntaje,
                score_prefiltro=f.score_final,
                recomendacion=ciclo.recomendacion,
                prompt_version=ciclo.prompt_version,
            )
        )
    return sorted(unidades, key=lambda u: (u.started_at, u.ciclo_id, u.symbol))


# --- Conteo de la muestra (modo conteo; no usa desenlaces) ---------------------------------


@dataclass(frozen=True)
class ConteoMuestra:
    ciclos: int
    ciclos_no_invertir: int
    ciclos_pareados: int
    dias_pareados: int
    incluidos: int
    no_incluidos: int
    por_nivel: dict[str, int]
    faltan: tuple[str, ...]

    @property
    def suficiente(self) -> bool:
        return not self.faltan


def _candidatas(unidades: Sequence[UnidadBase]) -> list[UnidadBase]:
    return [u for u in unidades if u.es_candidato]


def _agrupar_por_ciclo(unidades: Sequence[UnidadBase]) -> dict[str, list]:
    grupos: dict[str, list] = defaultdict(list)
    for u in unidades:
        grupos[u.ciclo_id].append(u)
    return grupos


def _ciclos_pareados(unidades: Sequence[UnidadBase]) -> dict[str, list]:
    return {
        c: g
        for c, g in _agrupar_por_ciclo(_candidatas(unidades)).items()
        if any(u.incluido for u in g) and any(u.no_incluido for u in g)
    }


def contar_muestra(unidades: Sequence[UnidadBase]) -> ConteoMuestra:
    """Cuenta la muestra con los mismos criterios de los mínimos del protocolo. Solo mira
    decisiones ya guardadas: no necesita velas ni desenlaces."""
    cand = _candidatas(unidades)
    ciclos = _agrupar_por_ciclo(cand)
    pareados = _ciclos_pareados(unidades)
    dias = {g[0].dia for g in pareados.values()}
    por_nivel = {n.name: sum(1 for u in cand if u.nivel == n) for n in NivelIA}
    incluidos = sum(1 for u in cand if u.incluido)
    faltan: list[str] = []
    if len(dias) < S1_MIN_DIAS_PAREADOS:
        faltan.append(f"días con ciclo pareado: {len(dias)} de {S1_MIN_DIAS_PAREADOS}")
    if len(pareados) < S1_MIN_CICLOS_PAREADOS:
        faltan.append(f"ciclos pareados: {len(pareados)} de {S1_MIN_CICLOS_PAREADOS}")
    if incluidos < S1_MIN_INCLUIDOS:
        faltan.append(f"candidatos incluidos por la IA: {incluidos} de {S1_MIN_INCLUIDOS}")
    return ConteoMuestra(
        ciclos=len(ciclos),
        ciclos_no_invertir=sum(1 for g in ciclos.values() if g[0].recomendacion == "no_invertir"),
        ciclos_pareados=len(pareados),
        dias_pareados=len(dias),
        incluidos=incluidos,
        no_incluidos=sum(1 for u in cand if u.no_incluido),
        por_nivel=por_nivel,
        faltan=tuple(faltan),
    )


# --- Estadística ----------------------------------------------------------------------------

Predicado = Callable[[UnidadValorada], bool]
Valor = Callable[[UnidadValorada], Decimal | None]


def _retorno(u: UnidadValorada) -> Decimal | None:
    return u.retorno_neto


def _media(xs: Sequence[Decimal]) -> Decimal:
    return sum(xs, _CERO) / len(xs)


def diferencias_pareadas_por_ciclo(
    unidades: Sequence[UnidadValorada], a: Predicado, b: Predicado, valor: Valor = _retorno
) -> dict[str, tuple[date, Decimal]]:
    """Por ciclo con unidades de AMBOS grupos: media del valor del grupo `a` menos la del
    grupo `b`. Los valores `None` no se rellenan: se omiten."""
    salida: dict[str, tuple[date, Decimal]] = {}
    for ciclo, g in _agrupar_por_ciclo(unidades).items():
        va = [v for u in g if a(u) and (v := valor(u)) is not None]
        vb = [v for u in g if b(u) and (v := valor(u)) is not None]
        if va and vb:
            salida[ciclo] = (g[0].dia, _media(va) - _media(vb))
    return salida


def serie_diaria(por_ciclo: dict[str, tuple[date, Decimal]]) -> list[tuple[date, Decimal]]:
    """Media por día UTC de los valores de sus ciclos, ordenada por fecha."""
    dias: dict[date, list[Decimal]] = defaultdict(list)
    for dia, v in por_ciclo.values():
        dias[dia].append(v)
    return sorted(((d, _media(v)) for d, v in dias.items()), key=lambda t: t[0])


def intervalo_de_serie(serie: Sequence[tuple[date, Decimal]]) -> IntervaloBootstrap | None:
    if not serie:
        return None
    return bootstrap_bloques(
        [v for _, v in serie], bloque=S1_BLOQUE_DIAS, iteraciones=S1_ITERACIONES, semilla=S1_SEMILLA
    )


def _percentiles(medias: list[Decimal], iteraciones: int) -> tuple[Decimal, Decimal]:
    medias.sort()
    cola = Fraction(100 - NIVEL_DE_CONFIANZA_PCT, 2 * 100)
    return medias[ceil(iteraciones * cola) - 1], medias[ceil(iteraciones * (1 - cola)) - 1]


def bootstrap_dif_de_medias_por_dias(
    dias: Sequence[tuple[Sequence[Decimal], Sequence[Decimal]]],
) -> IntervaloBootstrap | None:
    """Diferencia entre la media del grupo A y la del grupo B, con IC95 por bootstrap de
    BLOQUES de `S1_BLOQUE_DIAS` días consecutivos (cada elemento es un día: valores de A y
    de B de ese día). Una remuestra sin valores de A o sin valores de B se descarta y se
    vuelve a sortear (determinista con la semilla). `None` si algún grupo está vacío en
    toda la muestra."""
    n = len(dias)
    todos_a = [x for a, _ in dias for x in a]
    todos_b = [x for _, b in dias for x in b]
    if n == 0 or not todos_a or not todos_b:
        return None
    bloque = min(S1_BLOQUE_DIAS, n)
    rng = random.Random(S1_SEMILLA)
    medias: list[Decimal] = []
    intentos = 0
    while len(medias) < S1_ITERACIONES and intentos < S1_ITERACIONES * 20:
        intentos += 1
        va: list[Decimal] = []
        vb: list[Decimal] = []
        cuenta = 0
        while cuenta < n:
            inicio = rng.randrange(n - bloque + 1)
            for a, b in dias[inicio : inicio + bloque]:
                va.extend(a)
                vb.extend(b)
            cuenta += bloque
        if va and vb:
            medias.append(_media(va) - _media(vb))
    if len(medias) < S1_ITERACIONES:
        return None
    inf, sup = _percentiles(medias, S1_ITERACIONES)
    return IntervaloBootstrap(n, _media(todos_a) - _media(todos_b), inf, sup)


def ic_diario_por_ciclo(
    unidades: Sequence[UnidadValorada], x: Callable[[UnidadValorada], Decimal | None]
) -> list[tuple[date, Decimal]]:
    """RankIC (Spearman) entre `x` y el retorno neto, calculado dentro de cada ciclo y
    promediado por día UTC. Los ciclos donde la correlación no está definida (menos de 3
    pares o una serie sin variación) se omiten, no se rellenan con 0."""
    por_ciclo: dict[str, tuple[date, Decimal]] = {}
    for ciclo, g in _agrupar_por_ciclo(unidades).items():
        pares = [(xv, u.retorno_neto) for u in g if (xv := x(u)) is not None]
        ic = spearman([p[0] for p in pares], [p[1] for p in pares])
        if ic is not None:
            por_ciclo[ciclo] = (g[0].dia, ic)
    return serie_diaria(por_ciclo)


# --- Informe --------------------------------------------------------------------------------


class VeredictoS1(str, Enum):
    NO_CONCLUYENTE = "NO_CONCLUYENTE_MUESTRA_INSUFICIENTE"
    NO_DISCRIMINA = "NO_DISCRIMINA"
    DISCRIMINA = "DISCRIMINA"


@dataclass(frozen=True)
class SecundarioS1:
    clave: str
    descripcion: str
    intervalo: IntervaloBootstrap | None = None
    detalle: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class InformeS1:
    conteo: ConteoMuestra
    primario: IntervaloBootstrap | None
    veredicto: VeredictoS1
    secundarios: tuple[SecundarioS1, ...]
    notas: tuple[str, ...] = ()


def _veredicto(conteo: ConteoMuestra, primario: IntervaloBootstrap | None) -> VeredictoS1:
    if not conteo.suficiente or primario is None:
        return VeredictoS1.NO_CONCLUYENTE
    return VeredictoS1.DISCRIMINA if primario.ci_inferior > 0 else VeredictoS1.NO_DISCRIMINA


def _es_candidata(u: UnidadValorada) -> bool:
    return u.es_candidato


def _tasa(grupo: Sequence[UnidadValorada]) -> dict[str, str]:
    if not grupo:
        return {"n": "0", "con_retorno_positivo": "0", "proporcion": "—"}
    pos = sum(1 for u in grupo if u.retorno_neto > 0)
    return {"n": str(len(grupo)), "con_retorno_positivo": str(pos), "proporcion": f"{Decimal(pos) / len(grupo):.4f}"}


def _por_dia_de_dos_grupos(
    unidades: Sequence[UnidadValorada], a: Predicado, b: Predicado
) -> list[tuple[list[Decimal], list[Decimal]]]:
    dias: dict[date, tuple[list[Decimal], list[Decimal]]] = {}
    for u in sorted(unidades, key=lambda u: u.started_at):
        va, vb = dias.setdefault(u.dia, ([], []))
        if a(u):
            va.append(u.retorno_neto)
        if b(u):
            vb.append(u.retorno_neto)
    return [dias[d] for d in sorted(dias)]


def evaluar_s1(valoradas: Sequence[UnidadValorada]) -> InformeS1:
    """Aplica el protocolo a las unidades YA valoradas. El veredicto sigue la regla fija
    (mínimos de muestra y límite inferior del IC95 del contraste primario); los
    secundarios se calculan siempre y se reportan completos, favorezcan o no a la IA."""
    cand = [u for u in valoradas if u.es_candidato]
    conteo = contar_muestra(valoradas)
    incluido: Predicado = lambda u: u.incluido  # noqa: E731
    no_incluido: Predicado = lambda u: u.no_incluido  # noqa: E731

    serie = serie_diaria(diferencias_pareadas_por_ciclo(cand, incluido, no_incluido))
    primario = intervalo_de_serie(serie)

    sec: list[SecundarioS1] = []

    # 1. Ciclos no_invertir vs invertir: todos los candidatos de cada tipo de ciclo.
    sec.append(
        SecundarioS1(
            "no_invertir_vs_invertir",
            "Retorno medio de candidatos en ciclos «no_invertir» menos el de ciclos «invertir»",
            bootstrap_dif_de_medias_por_dias(
                _por_dia_de_dos_grupos(
                    cand, lambda u: u.recomendacion == "no_invertir", lambda u: u.recomendacion == "invertir"
                )
            ),
        )
    )
    # 2. Escala ordenada 0..3.
    sec.append(
        SecundarioS1(
            "escala_ordenada",
            "RankIC por ciclo entre el nivel de la IA (0 a 3) y el retorno, promediado por día",
            intervalo_de_serie(ic_diario_por_ciclo(cand, lambda u: Decimal(int(u.nivel)) if u.nivel is not None else None)),
        )
    )
    # 3. Puntaje dentro de lo incluido.
    sec.append(
        SecundarioS1(
            "puntaje_dentro_de_incluidos",
            "RankIC por ciclo entre el puntaje ponderado de la IA y el retorno (solo incluidos)",
            intervalo_de_serie(ic_diario_por_ciclo([u for u in cand if u.incluido], lambda u: u.puntaje_ia)),
        )
    )
    # 4. Candidatos vs resto de la lista autorizada.
    sec.append(
        SecundarioS1(
            "candidatos_vs_resto",
            "Retorno medio de candidatos menos el del resto de la lista autorizada, pareado por ciclo",
            intervalo_de_serie(
                serie_diaria(diferencias_pareadas_por_ciclo(valoradas, _es_candidata, lambda u: not u.es_candidato))
            ),
        )
    )
    # 5. Tasa de acierto descriptiva.
    sec.append(
        SecundarioS1(
            "tasa_de_acierto",
            "Proporción con retorno neto > 0 (descriptivo, sin inferencia)",
            None,
            {
                "incluidos": str(_tasa([u for u in cand if u.incluido])),
                "no_incluidos": str(_tasa([u for u in cand if u.no_incluido])),
            },
        )
    )
    # 6. Primario solo con salidas resueltas.
    resueltas = [u for u in cand if u.resuelta]
    sec.append(
        SecundarioS1(
            "primario_solo_resueltas",
            "Contraste primario usando solo candidatos cuya salida simulada ya se resolvió",
            intervalo_de_serie(serie_diaria(diferencias_pareadas_por_ciclo(resueltas, incluido, no_incluido))),
            {"unidades_resueltas": str(len(resueltas)), "unidades_abiertas": str(len(cand) - len(resueltas))},
        )
    )
    # 7. Confusión con el pre-filtro.
    sec.append(
        SecundarioS1(
            "confusion_prefiltro",
            "Diferencia pareada del score del pre-filtro entre incluidos y no incluidos "
            "(si es grande, el contraste primario puede reflejar el pre-filtro)",
            intervalo_de_serie(
                serie_diaria(
                    diferencias_pareadas_por_ciclo(cand, incluido, no_incluido, lambda u: u.score_prefiltro)
                )
            ),
        )
    )
    # 8. Composición.
    versiones: dict[str, int] = defaultdict(int)
    for c in {u.ciclo_id: u for u in cand}.values():
        versiones[str(c.prompt_version)] += 1
    sec.append(
        SecundarioS1(
            "composicion",
            "Conteos por nivel y por versión del prompt del Consolidador (ciclos)",
            None,
            {
                "por_nivel": str(conteo.por_nivel),
                "ciclos_por_prompt_version": str(dict(sorted(versiones.items()))),
                "ciclos": str(conteo.ciclos),
            },
        )
    )

    veredicto = _veredicto(conteo, primario)
    notas: tuple[str, ...] = ()
    if veredicto == VeredictoS1.NO_CONCLUYENTE:
        notas = ("Muestra insuficiente tras valorar los candidatos: " + "; ".join(conteo.faltan or ("sin ciclos pareados",)),)
    return InformeS1(conteo=conteo, primario=primario, veredicto=veredicto, secundarios=tuple(sec), notas=notas)
