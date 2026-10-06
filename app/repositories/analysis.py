"""Persistencia de `ciclos_analisis`, `propuestas` y `factores` (sección 6
del plan, Paso 7).

Solo persistencia (sección 15.2 del master prompt) — sin lógica de negocio:
no decide cuándo un ciclo pasa de una etapa a otra (eso es
`app/orchestration/daily_cycle.py`), no calcula scores (`app/domain/rules/
scoring.py`) y no arma el `decision_snapshot` (también orquestación). Solo
sabe escribir y leer filas.

`save_propuesta` inserta una `Propuesta` y sus `Factor` en la MISMA
transacción (un `flush` intermedio para obtener el `id` generado antes de
insertar los factores, sin `commit` — el `commit`/`rollback` de todo el
ciclo lo decide `session_scope`, igual que en el resto del proyecto): una
propuesta sin sus 4 factores, o factores huérfanos sin propuesta, sería un
estado a medias que rompe la trazabilidad que este paso existe para dar.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Date, and_, cast, func, select
from sqlalchemy.orm import Session

from app.db.models.analysis import CicloAnalisis, Factor, Propuesta
from app.domain.enums import EstadoCiclo, TierEntrada


# --- ciclos_analisis --------------------------------------------------------


def create_ciclo(session: Session, *, started_at: datetime, status: EstadoCiclo) -> uuid.UUID:
    """Crea la fila del ciclo en su primera etapa y devuelve su `id`.

    No hay clave de idempotencia natural (a diferencia de velas/
    indicadores): cada ejecución del orquestador es un evento nuevo. Evitar
    ciclos duplicados por reintento del propio orquestador (sección 18 del
    plan) es responsabilidad de quien orquesta, no de esta función."""
    ciclo = CicloAnalisis(started_at=started_at, status=status)
    session.add(ciclo)
    session.flush()
    return ciclo.id


def avanzar_ciclo(session: Session, ciclo_id: uuid.UUID, status: EstadoCiclo) -> None:
    """Actualiza `status` a la etapa alcanzada. No usa `finished_at`: el
    ciclo sigue en curso hasta `finalizar_ciclo`."""
    ciclo = session.get(CicloAnalisis, ciclo_id)
    if ciclo is None:
        raise ValueError(f"ciclo_id {ciclo_id} no existe")
    ciclo.status = status


def finalizar_ciclo(
    session: Session,
    ciclo_id: uuid.UUID,
    *,
    status: EstadoCiclo,
    finished_at: datetime,
    cost_usd: Decimal | None,
    error_message: str | None,
) -> None:
    """Cierra el ciclo: última etapa alcanzada, hora de fin, costo total
    acumulado (suma de `llamadas_ia.cost_usd` de este ciclo) y, si falló,
    el mensaje de error. `error_message=None` no implica que hubo
    propuestas — puede ser un final normal sin candidatos (ver el
    docstring de `EstadoCiclo` sobre cómo se deriva el resultado real)."""
    ciclo = session.get(CicloAnalisis, ciclo_id)
    if ciclo is None:
        raise ValueError(f"ciclo_id {ciclo_id} no existe")
    ciclo.status = status
    ciclo.finished_at = finished_at
    ciclo.cost_usd = cost_usd
    ciclo.error_message = error_message


def list_ciclos_recientes(session: Session, *, limit: int) -> list[CicloAnalisis]:
    """Los `limit` ciclos más recientes por inicio, del más nuevo al más viejo
    (desempate por `id`: orden reproducible)."""
    return list(
        session.execute(
            select(CicloAnalisis).order_by(CicloAnalisis.started_at.desc(), CicloAnalisis.id).limit(limit)
        ).scalars()
    )


def get_ciclo(session: Session, ciclo_id: uuid.UUID) -> CicloAnalisis | None:
    return session.get(CicloAnalisis, ciclo_id)


# --- propuestas / factores ---------------------------------------------------


@dataclass(frozen=True)
class FactorRow:
    """Uno de los 4 factores ponderados de una propuesta (decisión del
    usuario, 2026-09-30 — ver el docstring de `app.db.models.analysis`)."""

    symbol: str
    factor: str
    peso: Decimal
    puntaje: Decimal
    confianza: str | None


@dataclass(frozen=True)
class PropuestaRow:
    """Una opción del Consolidador, con su score ya calculado en código y
    su `decision_snapshot` ya congelado — lista para persistir junto con
    sus factores."""

    ciclo_id: uuid.UUID
    symbol: str
    categoria: str
    horizonte: str
    nivel_riesgo: str
    puntaje_base: Decimal
    calidad_datos: Decimal
    puntaje_ponderado: Decimal
    supera_umbral_entrada: bool
    tesis: str
    invalidacion: str
    escenario_adverso: dict
    comentario_chartista: str | None
    fuentes: list[str]
    confianza_global: str
    decision_snapshot: dict
    factores: tuple[FactorRow, ...]
    tier_entrada: TierEntrada | None = None
    segunda_ronda: dict | None = None
    ciclos_consecutivos_en_zona: int | None = None


def save_propuesta(session: Session, row: PropuestaRow) -> uuid.UUID:
    """Inserta la propuesta y sus factores en una sola unidad y devuelve el
    `id` de la propuesta."""
    propuesta = Propuesta(
        ciclo_id=row.ciclo_id,
        symbol=row.symbol,
        categoria=row.categoria,
        horizonte=row.horizonte,
        nivel_riesgo=row.nivel_riesgo,
        puntaje_base=row.puntaje_base,
        calidad_datos=row.calidad_datos,
        puntaje_ponderado=row.puntaje_ponderado,
        supera_umbral_entrada=row.supera_umbral_entrada,
        tesis=row.tesis,
        invalidacion=row.invalidacion,
        escenario_adverso=row.escenario_adverso,
        comentario_chartista=row.comentario_chartista,
        fuentes=row.fuentes,
        confianza_global=row.confianza_global,
        decision_snapshot=row.decision_snapshot,
        tier_entrada=row.tier_entrada,
        segunda_ronda=row.segunda_ronda,
        ciclos_consecutivos_en_zona=row.ciclos_consecutivos_en_zona,
    )
    session.add(propuesta)
    session.flush()  # necesitamos propuesta.id para los factores

    for factor_row in row.factores:
        session.add(
            Factor(
                ciclo_id=row.ciclo_id,
                propuesta_id=propuesta.id,
                symbol=factor_row.symbol,
                factor=factor_row.factor,
                peso=factor_row.peso,
                puntaje=factor_row.puntaje,
                confianza=factor_row.confianza,
            )
        )
    session.flush()
    return propuesta.id


def get_propuesta(session: Session, propuesta_id: uuid.UUID) -> Propuesta | None:
    return session.get(Propuesta, propuesta_id)


def get_propuestas_de_ciclo(session: Session, ciclo_id: uuid.UUID) -> list[Propuesta]:
    stmt = select(Propuesta).where(Propuesta.ciclo_id == ciclo_id).order_by(Propuesta.created_at)
    return list(session.scalars(stmt))


def get_factores_de_propuesta(session: Session, propuesta_id: uuid.UUID) -> list[Factor]:
    stmt = select(Factor).where(Factor.propuesta_id == propuesta_id).order_by(Factor.factor)
    return list(session.scalars(stmt))


def get_puntajes_de_ciclos_previos(session: Session, symbol: str, ciclo_id: uuid.UUID) -> list[Decimal | None]:
    """`puntaje_ponderado` de `symbol` en cada DÍA de análisis anterior al
    del ciclo `ciclo_id`, del más reciente al más antiguo. Solo
    persistencia: no interpreta la lista (eso es
    `app.domain.rules.entry_tier.contar_ciclos_consecutivos_en_zona`).

    - Solo cuentan ciclos que terminaron (`finished_at`) sin error
      (`error_message` nulo): un ciclo fallido no produjo información.
    - Se agrupa por día UTC y solo entran días ESTRICTAMENTE anteriores al
      del ciclo actual, para que una segunda corrida el mismo día no cuente
      como "el día anterior". Si hubo varios ciclos exitosos el mismo día,
      se toma el mayor puntaje del símbolo.
    - Un día con ciclo exitoso donde el símbolo no tuvo propuesta aparece
      como `None` (no se sabe si estuvo cerca; ver el docstring de la
      función de dominio)."""
    actual = session.get(CicloAnalisis, ciclo_id)
    if actual is None:
        raise ValueError(f"ciclo_id {ciclo_id} no existe")

    dia = cast(func.timezone("UTC", CicloAnalisis.started_at), Date)
    dia_actual = cast(func.timezone("UTC", actual.started_at), Date)
    stmt = (
        select(func.max(Propuesta.puntaje_ponderado))
        .select_from(CicloAnalisis)
        .outerjoin(Propuesta, and_(Propuesta.ciclo_id == CicloAnalisis.id, Propuesta.symbol == symbol))
        .where(
            CicloAnalisis.finished_at.is_not(None),
            CicloAnalisis.error_message.is_(None),
            dia < dia_actual,
        )
        .group_by(dia)
        .order_by(dia.desc())
    )
    return [None if valor is None else Decimal(valor) for valor in session.execute(stmt).scalars()]
