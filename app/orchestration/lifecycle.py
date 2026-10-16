"""Recuperación al arrancar el proceso operativo (Paso 11c; principio P5).

    START -> LOAD DATABASE STATE -> RECONCILE -> VALIDATE -> ONLY THEN ALLOW NEW ACTIONS

`run_startup_recovery` se llama UNA vez al arrancar el scheduler, antes de
registrar los jobs. Hace tres cosas, cada una en su propia transacción y
protegida: si una falla se registra y el arranque sigue (nunca impide vigilar
las posiciones abiertas).

1. Marca como INTERRUMPIDOS los ciclos de análisis sin terminar
   (`finished_at` vacío): el proceso murió a mitad. Se les pone fin, su última
   etapa se conserva y el costo real de IA ya gastado (suma de `llamadas_ia`)
   queda registrado. Nada se relanza ni se borra. Es seguro porque el
   scheduler es un proceso único (`app.repositories.process_lock`); y si se
   marcara por error un ciclo que seguía vivo, su propio `finalizar_ciclo`
   sobrescribiría esos campos al terminar.
2. Reconcilia órdenes y posiciones. Si no cuadran, deja un aviso
   (`DISCREPANCIA_RECONCILIACION`). No corrige nada: "bloquear operación hasta
   resolver" lo hacen el monitor (se detiene en cada chequeo) y la apertura de
   entradas (`app.execution.order_manager`, que reconcilia antes de actuar).
   Por eso "solo entonces permitir acciones nuevas" no depende de que este
   paso haya corrido: se hace respetar en el momento de cada acción.
3. Si ya pasó la hora del ciclo diario de hoy y no hay un ciclo completo,
   deja un aviso (`CICLO_DIARIO_PERDIDO`). NO lo relanza (decisión del usuario
   del 2026-10-10; ver `app.domain.rules.recovery`).

Los avisos solo se ESCRIBEN aquí; los envía el despachador después (P2).
Todos son idempotentes: reiniciar varias veces no repite avisos.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.domain.enums import TipoAlerta
from app.domain.rules.recovery import (
    CicloParaRecuperar,
    MENSAJE_CICLO_INTERRUMPIDO,
    hay_ciclo_diario_perdido,
    inicio_del_dia_utc,
    mensaje_ciclo_diario_perdido,
)
from app.execution.escalations import avisar_discrepancia
from app.execution.reconciliation import reconcile
from app.repositories.ai_calls import list_llamadas_de_ciclo
from app.repositories.alertas import AlertaRow, append_alerta
from app.repositories.analysis import finalizar_ciclo, list_ciclos_iniciados_desde, list_ciclos_sin_terminar

logger = logging.getLogger(__name__)

_COMPONENT = "orchestration.lifecycle"
_SISTEMA = "SISTEMA"


@dataclass(frozen=True)
class StartupReport:
    ciclos_interrumpidos: tuple[uuid.UUID, ...] = ()
    discrepancias: int = 0
    ciclo_diario_perdido: bool = False
    avisos_creados: tuple[TipoAlerta, ...] = ()
    errores: tuple[str, ...] = ()
    """Pasos de la recuperación que fallaron (el arranque continúa igual)."""

    @property
    def estado_sano(self) -> bool:
        return not (self.discrepancias or self.errores)


def interrumpir_ciclos_huerfanos(session: Session, *, now: datetime) -> tuple[uuid.UUID, ...]:
    """Marca como interrumpido cada ciclo sin terminar (ver el docstring del
    módulo). Devuelve los `id` marcados."""
    marcados: list[uuid.UUID] = []
    for ciclo in list_ciclos_sin_terminar(session):
        gastado = sum((c.cost_usd for c in list_llamadas_de_ciclo(session, ciclo.id)), Decimal(0))
        finalizar_ciclo(
            session,
            ciclo.id,
            status=ciclo.status,
            finished_at=now,
            cost_usd=gastado,
            error_message=MENSAJE_CICLO_INTERRUMPIDO,
        )
        marcados.append(ciclo.id)
        logger.warning(
            "ciclo huérfano marcado como interrumpido",
            extra={
                "component": _COMPONENT,
                "ciclo_id": str(ciclo.id),
                "ultima_etapa": ciclo.status.value,
                "costo_ia_usd": str(gastado),
            },
        )
    return tuple(marcados)


def avisar_ciclo_diario_perdido(session: Session, *, now: datetime, interrumpidos: int) -> bool:
    """Un aviso por día UTC, aunque el scheduler se reinicie varias veces."""
    dia = inicio_del_dia_utc(now).date()
    return append_alerta(
        session,
        AlertaRow(
            tipo=TipoAlerta.CICLO_DIARIO_PERDIDO,
            subject=_SISTEMA,
            dedup_key=f"ciclo-diario-perdido:{dia.isoformat()}",
            occurred_at=now,
            message=mensaje_ciclo_diario_perdido(dia, interrumpidos=interrumpidos),
        ),
    )


def run_startup_recovery(session_factory: sessionmaker[Session], *, now: datetime) -> StartupReport:
    """Recuperación completa al arrancar (ver el docstring del módulo)."""
    interrumpidos: tuple[uuid.UUID, ...] = ()
    discrepancias = 0
    perdido = False
    avisos: list[TipoAlerta] = []
    errores: list[str] = []

    try:
        with session_scope(session_factory) as session:
            interrumpidos = interrumpir_ciclos_huerfanos(session, now=now)
    except Exception as exc:  # noqa: BLE001 - el arranque sigue; se informa en el reporte
        errores.append(f"ciclos huérfanos: {type(exc).__name__}: {exc}")
        logger.error("recuperación: falló el marcado de ciclos huérfanos", exc_info=True, extra={"component": _COMPONENT})

    try:
        with session_scope(session_factory) as session:
            hallazgos = reconcile(session)
            discrepancias = len(hallazgos)
            if hallazgos:
                logger.critical(
                    "recuperación: el estado de órdenes y posiciones NO cuadra",
                    extra={
                        "component": _COMPONENT,
                        "discrepancias": [f"{d.codigo.value}: {d.detalle}" for d in hallazgos],
                    },
                )
                if avisar_discrepancia(session, now=now, discrepancias=hallazgos):
                    avisos.append(TipoAlerta.DISCREPANCIA_RECONCILIACION)
    except Exception as exc:  # noqa: BLE001
        errores.append(f"reconciliación: {type(exc).__name__}: {exc}")
        logger.error("recuperación: falló la reconciliación", exc_info=True, extra={"component": _COMPONENT})

    try:
        with session_scope(session_factory) as session:
            ciclos = [
                CicloParaRecuperar(c.id, c.started_at, c.finished_at, c.error_message)
                for c in list_ciclos_iniciados_desde(session, inicio_del_dia_utc(now))
            ]
            perdido = hay_ciclo_diario_perdido(ciclos, now=now)
            if perdido:
                logger.warning(
                    "recuperación: no hay un ciclo diario completo de hoy",
                    extra={"component": _COMPONENT},
                )
                if avisar_ciclo_diario_perdido(session, now=now, interrumpidos=len(interrumpidos)):
                    avisos.append(TipoAlerta.CICLO_DIARIO_PERDIDO)
    except Exception as exc:  # noqa: BLE001
        errores.append(f"ciclo diario: {type(exc).__name__}: {exc}")
        logger.error("recuperación: falló la revisión del ciclo diario", exc_info=True, extra={"component": _COMPONENT})

    reporte = StartupReport(
        ciclos_interrumpidos=interrumpidos,
        discrepancias=discrepancias,
        ciclo_diario_perdido=perdido,
        avisos_creados=tuple(avisos),
        errores=tuple(errores),
    )
    logger.info(
        "recuperación al arrancar terminada",
        extra={
            "component": _COMPONENT,
            "ciclos_interrumpidos": len(interrumpidos),
            "discrepancias": discrepancias,
            "ciclo_diario_perdido": perdido,
            "avisos": [a.value for a in avisos],
            "errores": len(errores),
        },
    )
    return reporte
