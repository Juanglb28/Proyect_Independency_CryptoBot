"""Decisiones y análisis: trazabilidad de cada ciclo (secciones 8.9 y 15.6).

Muestra, para cada ciclo, qué se evaluó, qué recomendó el Consolidador, qué
propuestas salieron (con sus factores y el criterio de entrada) y si alguna
llegó a ser orden y posición. Todo es lo que quedó guardado entonces.

El resultado del pre-filtro de cada ciclo (qué activos del universo pasaron
el corte y con qué puntaje) se guarda desde 2026-10-07 en
`prefiltro_resultados`; aquí solo se lee. Limitación conocida, mostrada tal
cual en la interfaz: los ciclos ANTERIORES a esa fecha no lo tienen (no se
rellena retroactivamente: recalcular con los datos de hoy sería mezclar el
presente con una decisión pasada), así que sus "candidatos evaluados" se
reconstruyen de las llamadas al Analista cuantitativo.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy.orm import Session

from app.domain.enums import EstadoCiclo, EstadoOrden, EstadoPosicion, EstadoPrefiltro, TierEntrada
from app.repositories.ai_calls import list_llamadas_de_ciclo
from app.repositories.analysis import (
    get_ciclo,
    get_factores_de_propuesta,
    get_propuestas_de_ciclo,
    list_ciclos_recientes,
    list_prefiltro_de_ciclo,
)
from app.repositories.orders import list_orders_for_proposal
from app.repositories.positions import get_posicion_by_proposal
from app.services.trades import FactorView

AGENTE_CUANTITATIVO = "analista_cuantitativo"
AGENTE_CONSOLIDADOR = "consolidador"

NOTA_EMBUDO = (
    "Este ciclo es anterior al registro del pre-filtro: no se guardó qué activos del universo pasaron el corte, "
    "y los candidatos se reconstruyen de las llamadas al Analista cuantitativo."
)
NOTA_EMBUDO_REGISTRADO = (
    "El pre-filtro de este ciclo está guardado tal como se calculó ese día (con la versión de fórmula indicada); "
    "los candidatos evaluados por la IA salen de las llamadas al Analista cuantitativo."
)


class ResultadoCiclo(str, Enum):
    """Derivado de `status`, `error_message`, `finished_at` y las propuestas,
    como documenta `EstadoCiclo` (nunca un valor guardado aparte)."""

    EN_CURSO = "EN_CURSO"
    FALLIDO = "FALLIDO"
    CON_PROPUESTAS = "CON_PROPUESTAS"
    SIN_PROPUESTAS = "SIN_PROPUESTAS"


@dataclass(frozen=True)
class CicloResumen:
    ciclo_id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None
    ultima_etapa: EstadoCiclo
    resultado: ResultadoCiclo
    cost_usd: Decimal | None
    error_message: str | None
    n_propuestas: int
    """Propuestas de la ESTRATEGIA (con IA). Las de observación se cuentan aparte."""
    n_sobre_umbral: int
    n_observaciones: int = 0
    """Posiciones de observación abiertas por este ciclo (candidatos del pre-filtro, sin IA)."""


@dataclass(frozen=True)
class PropuestaDelCiclo:
    proposal_id: uuid.UUID
    symbol: str
    categoria: str
    horizonte: str
    nivel_riesgo: str
    puntaje_base: Decimal
    calidad_datos: Decimal | None
    """`None` en las observaciones: no pasaron por el Auditor y no se inventa."""
    puntaje_ponderado: Decimal
    supera_umbral_entrada: bool
    tier_entrada: str | None
    ciclos_consecutivos_en_zona: int | None
    segunda_ronda: dict | None
    confianza_global: str
    tesis: str
    invalidacion: str
    escenario_adverso: dict
    comentario_chartista: str | None
    fuentes: tuple[str, ...]
    factores: tuple[FactorView, ...]
    estado_entrada: str | None
    """Estado de la orden de entrada (PENDING_APPROVAL, FILLED, REJECTED,
    EXPIRED...) o `None` si esta propuesta nunca generó una orden."""
    position_id: uuid.UUID | None
    estado_posicion: str | None


@dataclass(frozen=True)
class LlamadaIAView:
    agente: str
    modelo: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    exitosa: bool
    error_message: str | None
    created_at: datetime


@dataclass(frozen=True)
class PrefiltroSimbolo:
    """Resultado del pre-filtro de un símbolo en un ciclo, tal como se guardó.
    Los números son `None` si el símbolo quedó `SIN_DATOS` (nunca 0)."""

    symbol: str
    estado: EstadoPrefiltro
    ranking: int | None
    tendencia_pct: Decimal | None
    volumen_relativo: Decimal | None
    distancia_max_90d_pct: Decimal | None
    score_final: Decimal | None
    formula_version: str


@dataclass(frozen=True)
class CicloDetalle:
    resumen: CicloResumen
    candidatos_evaluados: tuple[str, ...]
    recomendacion: str | None
    razon_no_invertir: str | None
    confianza_global: str | None
    propuestas: tuple[PropuestaDelCiclo, ...]
    llamadas: tuple[LlamadaIAView, ...]
    prefiltro: tuple[PrefiltroSimbolo, ...] = ()
    """Vacío en los ciclos anteriores al registro del pre-filtro."""
    nota_embudo: str = NOTA_EMBUDO


def _resultado(ciclo, n_propuestas: int) -> ResultadoCiclo:
    if ciclo.error_message:
        return ResultadoCiclo.FALLIDO
    if ciclo.finished_at is None:
        return ResultadoCiclo.EN_CURSO
    return ResultadoCiclo.CON_PROPUESTAS if n_propuestas > 0 else ResultadoCiclo.SIN_PROPUESTAS


def _resumen(session: Session, ciclo) -> CicloResumen:
    todas = get_propuestas_de_ciclo(session, ciclo.id)
    propuestas = [p for p in todas if p.tier_entrada != TierEntrada.OBSERVACION]
    return CicloResumen(
        ciclo_id=ciclo.id,
        started_at=ciclo.started_at,
        finished_at=ciclo.finished_at,
        ultima_etapa=ciclo.status,
        resultado=_resultado(ciclo, len(propuestas)),
        cost_usd=ciclo.cost_usd,
        error_message=ciclo.error_message,
        n_propuestas=len(propuestas),
        n_sobre_umbral=sum(1 for p in propuestas if p.supera_umbral_entrada),
        n_observaciones=len(todas) - len(propuestas),
    )


def list_ciclos(session: Session, *, limit: int = 30) -> tuple[CicloResumen, ...]:
    """Los ciclos más recientes, el más nuevo primero."""
    return tuple(_resumen(session, c) for c in list_ciclos_recientes(session, limit=limit))


def _propuesta(session: Session, p) -> PropuestaDelCiclo:
    ordenes = list_orders_for_proposal(session, p.id)
    entrada = next((o for o in ordenes if o.intencion.value == "ENTRY"), None)
    posicion = get_posicion_by_proposal(session, p.id)
    return PropuestaDelCiclo(
        proposal_id=p.id,
        symbol=p.symbol,
        categoria=p.categoria,
        horizonte=p.horizonte,
        nivel_riesgo=p.nivel_riesgo,
        puntaje_base=p.puntaje_base,
        calidad_datos=p.calidad_datos,
        puntaje_ponderado=p.puntaje_ponderado,
        supera_umbral_entrada=p.supera_umbral_entrada,
        tier_entrada=None if p.tier_entrada is None else p.tier_entrada.value,
        ciclos_consecutivos_en_zona=p.ciclos_consecutivos_en_zona,
        segunda_ronda=p.segunda_ronda,
        confianza_global=p.confianza_global,
        tesis=p.tesis,
        invalidacion=p.invalidacion,
        escenario_adverso=p.escenario_adverso,
        comentario_chartista=p.comentario_chartista,
        fuentes=tuple(p.fuentes),
        factores=tuple(
            FactorView(f.factor, f.peso, f.puntaje, f.confianza) for f in get_factores_de_propuesta(session, p.id)
        ),
        estado_entrada=None if entrada is None else _valor(entrada.estado),
        position_id=None if posicion is None else posicion.id,
        estado_posicion=None if posicion is None else _valor(posicion.status),
    )


def _valor(valor: EstadoOrden | EstadoPosicion) -> str:
    return valor.value


def get_ciclo_detalle(session: Session, ciclo_id: uuid.UUID) -> CicloDetalle | None:
    ciclo = get_ciclo(session, ciclo_id)
    if ciclo is None:
        return None
    llamadas = list_llamadas_de_ciclo(session, ciclo_id)

    candidatos: list[str] = []
    for ll in llamadas:
        if ll.agente == AGENTE_CUANTITATIVO and ll.exitosa and ll.respuesta:
            simbolo = ll.respuesta.get("simbolo")
            if isinstance(simbolo, str) and simbolo not in candidatos:
                candidatos.append(simbolo)

    consolidador = next(
        (ll for ll in reversed(llamadas) if ll.agente == AGENTE_CONSOLIDADOR and ll.exitosa and ll.respuesta), None
    )
    respuesta = consolidador.respuesta if consolidador is not None else {}
    prefiltro = tuple(
        PrefiltroSimbolo(
            symbol=r.symbol,
            estado=r.estado,
            ranking=r.ranking,
            tendencia_pct=r.tendencia_pct,
            volumen_relativo=r.volumen_relativo,
            distancia_max_90d_pct=r.distancia_max_90d_pct,
            score_final=r.score_final,
            formula_version=r.formula_version,
        )
        for r in list_prefiltro_de_ciclo(session, ciclo_id)
    )
    return CicloDetalle(
        resumen=_resumen(session, ciclo),
        candidatos_evaluados=tuple(candidatos),
        recomendacion=respuesta.get("recomendacion"),
        razon_no_invertir=respuesta.get("razon_no_invertir"),
        confianza_global=respuesta.get("confianza_global"),
        propuestas=tuple(_propuesta(session, p) for p in get_propuestas_de_ciclo(session, ciclo_id)),
        prefiltro=prefiltro,
        nota_embudo=NOTA_EMBUDO_REGISTRADO if prefiltro else NOTA_EMBUDO,
        llamadas=tuple(
            LlamadaIAView(
                agente=ll.agente,
                modelo=ll.modelo,
                prompt_version=ll.prompt_version,
                input_tokens=ll.input_tokens,
                output_tokens=ll.output_tokens,
                cost_usd=ll.cost_usd,
                exitosa=ll.exitosa,
                error_message=ll.error_message,
                created_at=ll.created_at,
            )
            for ll in llamadas
        ),
    )
