"""Apertura AUTOMÁTICA de entradas paper al terminar un ciclo de análisis
(decisión del usuario, 2026-10-12).

## Qué cambia

Hasta ahora nada abría posiciones por sí solo: el scheduler corría el ciclo y
guardaba las propuestas, y abrir entradas exigía lanzar a mano
`scripts.entries open --ciclo-id`. Por eso, en semanas de corridas, no hubo una
sola posición. Este paso se llama después de cada ciclo de análisis (el diario y
el disparado por candidatos nuevos) y hace dos cosas, siempre con las MISMAS
reglas de riesgo (`app/domain/rules/risk.py`):

1. **Estrategia**: las propuestas del ciclo que calificaron (`NORMAL` entra sola
   con 100 USD; `MAYOR_RIESGO` queda pendiente de tu aprobación). Nada nuevo:
   solo conecta lo ya decidido en el Paso 8.
2. **Observación**: cada candidato del pre-filtro (estado `CANDIDATO`, puntaje
   >= 45) que la estrategia no abrió abre una posición paper de 100 USD, sin IA,
   con cupos propios, fuera del equity y medida como serie aparte. Ver
   `app/domain/rules/observation.py`.

## Garantías

- **Sin IA en el camino de la orden (P2)**: este módulo no importa agentes. El
  puntaje de una observación es el del pre-filtro, ya guardado.
- **Idempotente (P5)**: el id de la propuesta de observación es determinista y
  el `client_order_id` de la orden sale de ese id; reintentar el paso del mismo
  ciclo no crea otra propuesta ni otra posición.
- **Sin propuestas huérfanas**: solo se guarda la propuesta de una observación
  que de verdad va a abrirse (la selección se hace primero, en seco, con la
  misma función pura que usa `open_entries`). Las observaciones descartadas
  (cupos, símbolo ya abierto, pausa, `PARAR`...) se informan con su motivo pero
  no dejan fila.
- **Una transacción, ningún `commit` aquí**: lo hace quien llama.
- **Bloqueo por reconciliación**: si el estado no cuadra no se abre nada.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.enums import EstadoPrefiltro, SerieOperacion, TierEntrada
from app.domain.rules.execution import CandidatoEntrada, select_entries
from app.domain.rules.observation import (
    CATEGORIA_OBSERVACION,
    CONFIANZA_OBSERVACION,
    HORIZONTE_OBSERVACION,
    NIVEL_RIESGO_OBSERVACION,
    DatosPrefiltro,
    PropuestaDeObservacion,
    build_observation_proposal,
)
from app.execution.emergency_stop import EmergencyStop
from app.execution.order_manager import (
    EntradaProcesada,
    build_cupos,
    candidates_from_cycle,
    expire_pending_entries,
    open_entries,
)
from app.execution.price_source import PriceSource
from app.execution.reconciliation import Discrepancia, reconcile
from app.repositories.analysis import PropuestaRow, insertar_propuesta_observacion, list_prefiltro_de_ciclo

logger = logging.getLogger(__name__)

_COMPONENT = "orchestration.entry_opening"


@dataclass(frozen=True)
class EntradaDelCiclo:
    """Qué pasó con un candidato al intentar abrirle una entrada."""

    symbol: str
    serie: SerieOperacion
    puntaje: Decimal
    """Puntaje ponderado (estrategia) o del pre-filtro (observación)."""
    resultado: str
    """`ABIERTA`, `PENDIENTE_APROBACION`, `YA_PROCESADA`, `SIN_PRECIO` o `DESCARTADA`."""
    motivos: tuple[str, ...] = ()
    """Solo si `DESCARTADA`: los motivos de rechazo de `risk.validate_entry`."""
    orden_id: uuid.UUID | None = None
    posicion_id: uuid.UUID | None = None


@dataclass(frozen=True)
class AperturaDelCiclo:
    ciclo_id: uuid.UUID
    entradas: tuple[EntradaDelCiclo, ...]
    expiradas: int = 0
    discrepancias: tuple[Discrepancia, ...] = ()

    @property
    def bloqueada_por_reconciliacion(self) -> bool:
        return bool(self.discrepancias)

    def de_serie(self, serie: SerieOperacion) -> tuple[EntradaDelCiclo, ...]:
        return tuple(e for e in self.entradas if e.serie == serie)


def _datos_de_prefiltro(row) -> DatosPrefiltro:
    return DatosPrefiltro(
        symbol=row.symbol,
        estado=row.estado,
        ranking=row.ranking,
        calculated_at=row.calculated_at,
        tendencia_pct=row.tendencia_pct,
        volumen_relativo=row.volumen_relativo,
        distancia_max_90d_pct=row.distancia_max_90d_pct,
        score_tendencia=row.score_tendencia,
        score_volumen=row.score_volumen,
        score_distancia=row.score_distancia,
        score_final=row.score_final,
        formula_version=row.formula_version,
    )


def _fila_de_propuesta(p: PropuestaDeObservacion) -> PropuestaRow:
    return PropuestaRow(
        ciclo_id=p.ciclo_id,
        symbol=p.symbol,
        categoria=CATEGORIA_OBSERVACION,
        horizonte=HORIZONTE_OBSERVACION,
        nivel_riesgo=NIVEL_RIESGO_OBSERVACION,
        puntaje_base=p.puntaje,
        calidad_datos=None,  # sin Auditor: no se inventa
        puntaje_ponderado=p.puntaje,
        supera_umbral_entrada=False,
        tesis=p.tesis,
        invalidacion=p.invalidacion,
        escenario_adverso={},
        comentario_chartista=None,
        fuentes=[],
        confianza_global=CONFIANZA_OBSERVACION,
        decision_snapshot=p.decision_snapshot,
        factores=(),
        tier_entrada=TierEntrada.OBSERVACION,
    )


def _candidatos_de_observacion(session: Session, ciclo_id: uuid.UUID) -> list[PropuestaDeObservacion]:
    """Un candidato de observación por cada símbolo `CANDIDATO` del pre-filtro del
    ciclo (aún sin guardar nada)."""
    return [
        build_observation_proposal(ciclo_id, _datos_de_prefiltro(row))
        for row in list_prefiltro_de_ciclo(session, ciclo_id)
        if row.estado == EstadoPrefiltro.CANDIDATO and row.score_final is not None
    ]


def abrir_entradas_del_ciclo(
    session: Session,
    ciclo_id: uuid.UUID,
    *,
    now: datetime,
    price_source: PriceSource,
    emergency_stop: EmergencyStop | None = None,
) -> AperturaDelCiclo:
    """Abre las entradas de estrategia y de observación de un ciclo (ver el
    docstring del módulo). No hace `commit`."""
    discrepancias = reconcile(session)
    if discrepancias:
        logger.critical(
            "apertura de entradas bloqueada por reconciliación (%d discrepancia(s))",
            len(discrepancias),
            extra={"component": _COMPONENT, "ciclo_id": str(ciclo_id)},
        )
        return AperturaDelCiclo(ciclo_id, (), discrepancias=tuple(discrepancias))
    expiradas = expire_pending_entries(session, now)

    estrategia = candidates_from_cycle(session, ciclo_id)
    observaciones = {p.id: p for p in _candidatos_de_observacion(session, ciclo_id)}
    candidatos_obs = [
        CandidatoEntrada(proposal_id=p.id, symbol=p.symbol, tier=TierEntrada.OBSERVACION, puntaje_ponderado=p.puntaje)
        for p in observaciones.values()
    ]

    # Selección EN SECO con la misma función pura de `open_entries`: así solo se
    # guarda la propuesta de la observación que realmente va a abrirse.
    cupos = build_cupos(session, now, emergency_stop=emergency_stop)
    previa = select_entries(estrategia + candidatos_obs, cupos)
    ids_obs_elegidas = {
        s.candidato.proposal_id for s in previa.seleccionadas if s.candidato.tier == TierEntrada.OBSERVACION
    }
    obs_descartadas = tuple(d for d in previa.descartadas if d.candidato.tier == TierEntrada.OBSERVACION)

    elegidas_obs: list[CandidatoEntrada] = []
    for candidato in candidatos_obs:
        if candidato.proposal_id in ids_obs_elegidas:
            propuesta = observaciones[candidato.proposal_id]
            insertar_propuesta_observacion(session, _fila_de_propuesta(propuesta), propuesta_id=propuesta.id)
            elegidas_obs.append(candidato)

    resultado = open_entries(
        session, estrategia + elegidas_obs, now=now, price_source=price_source, emergency_stop=emergency_stop
    )
    if resultado.bloqueada_por_reconciliacion:  # pragma: no cover - ya se reconcilió arriba
        return AperturaDelCiclo(ciclo_id, (), expiradas=resultado.expiradas, discrepancias=resultado.discrepancias)

    por_id = {c.proposal_id: c for c in estrategia + candidatos_obs}
    entradas: list[EntradaDelCiclo] = []

    def _procesada(p: EntradaProcesada) -> EntradaDelCiclo:
        c = por_id[p.proposal_id]
        return EntradaDelCiclo(
            symbol=p.symbol,
            serie=SerieOperacion.OBSERVACION if c.tier == TierEntrada.OBSERVACION else SerieOperacion.ESTRATEGIA,
            puntaje=c.puntaje_ponderado,
            resultado=p.resultado.value,
            orden_id=p.orden_id,
            posicion_id=p.posicion_id,
        )

    entradas.extend(_procesada(p) for p in resultado.procesadas)
    for d in tuple(resultado.descartadas) + obs_descartadas:
        c = d.candidato
        entradas.append(
            EntradaDelCiclo(
                symbol=c.symbol,
                serie=SerieOperacion.OBSERVACION if c.tier == TierEntrada.OBSERVACION else SerieOperacion.ESTRATEGIA,
                puntaje=c.puntaje_ponderado,
                resultado="DESCARTADA",
                motivos=tuple(m.value for m in d.motivos),
            )
        )

    logger.info(
        "entradas del ciclo: %d abiertas, %d pendientes, %d descartadas",
        sum(1 for e in entradas if e.resultado == "ABIERTA"),
        sum(1 for e in entradas if e.resultado == "PENDIENTE_APROBACION"),
        sum(1 for e in entradas if e.resultado == "DESCARTADA"),
        extra={"component": _COMPONENT, "ciclo_id": str(ciclo_id)},
    )
    return AperturaDelCiclo(ciclo_id, tuple(entradas), expiradas=expiradas)
