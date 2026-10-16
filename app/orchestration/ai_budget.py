"""Guardia del tope diario de gasto de IA para los ciclos AUTOMÁTICOS.

Ver `app.domain.rules.ai_budget` para la semántica (tope blando, antes de
iniciar el ciclo). Aquí solo se lee el gasto del día UTC, se decide con la regla
pura y, si está agotado, se deja UN aviso por día en `alertas` (idempotente por
`dedup_key`; reiniciar el scheduler no lo repite). El envío a Telegram lo hace
el despachador después.

Falla CERRADO: si no se puede leer el gasto, no se inicia el ciclo. Gastar IA a
ciegas es peor que perder un ciclo, que se puede lanzar a mano.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.domain.enums import TipoAlerta
from app.domain.rules.ai_budget import (
    mensaje_presupuesto_ia_agotado,
    mensaje_saldo_ia_insuficiente,
    presupuesto_diario_agotado,
    saldo_estimado_usd,
    saldo_insuficiente,
)
from app.domain.rules.recovery import inicio_del_dia_utc
from app.repositories.ai_calls import get_gasto_acumulado_desde
from app.repositories.alertas import AlertaRow, append_alerta

logger = logging.getLogger(__name__)

_COMPONENT = "orchestration.ai_budget"


@dataclass(frozen=True)
class EstadoPresupuestoIA:
    dia: date
    gasto_hoy_usd: Decimal
    tope_usd: Decimal

    @property
    def agotado(self) -> bool:
        return presupuesto_diario_agotado(self.gasto_hoy_usd, self.tope_usd)


def leer_presupuesto_ia(session: Session, *, tope_usd: Decimal, now: datetime) -> EstadoPresupuestoIA:
    inicio = inicio_del_dia_utc(now)
    return EstadoPresupuestoIA(
        dia=inicio.date(),
        gasto_hoy_usd=get_gasto_acumulado_desde(session, inicio),
        tope_usd=tope_usd,
    )


def avisar_presupuesto_ia_agotado(session: Session, estado: EstadoPresupuestoIA, *, now: datetime) -> bool:
    """Un aviso por día UTC. `True` si lo escribió, `False` si ya existía."""
    return append_alerta(
        session,
        AlertaRow(
            tipo=TipoAlerta.PRESUPUESTO_IA_AGOTADO,
            subject="SISTEMA",
            dedup_key=f"presupuesto-ia-agotado:{estado.dia.isoformat()}",
            occurred_at=now,
            message=mensaje_presupuesto_ia_agotado(
                estado.dia, gasto_hoy_usd=estado.gasto_hoy_usd, tope_usd=estado.tope_usd
            ),
            observed_value=estado.gasto_hoy_usd,
            threshold=estado.tope_usd,
        ),
    )


def verificar_presupuesto_ia(
    session_factory: sessionmaker[Session], *, tope_usd: Decimal, now: datetime
) -> bool:
    """`True` si se puede iniciar un ciclo automático con IA. `False` si el tope
    de hoy está agotado (deja el aviso) o si no se pudo leer el gasto."""
    try:
        with session_scope(session_factory) as session:
            estado = leer_presupuesto_ia(session, tope_usd=tope_usd, now=now)
            if not estado.agotado:
                logger.info(
                    "presupuesto de IA: gasto de hoy %s de %s USD",
                    estado.gasto_hoy_usd,
                    estado.tope_usd,
                    extra={"component": _COMPONENT},
                )
                return True
            creado = avisar_presupuesto_ia_agotado(session, estado, now=now)
    except Exception:  # noqa: BLE001 - falla cerrado, ver el docstring del módulo
        logger.error(
            "presupuesto de IA: no se pudo leer el gasto; el ciclo NO se inicia",
            exc_info=True,
            extra={"component": _COMPONENT},
        )
        return False
    logger.warning(
        "presupuesto de IA AGOTADO: gasto de hoy %s de %s USD; el ciclo NO se inicia (aviso nuevo: %s)",
        estado.gasto_hoy_usd,
        estado.tope_usd,
        creado,
        extra={"component": _COMPONENT},
    )
    return False


# ── Guardia de SALDO (2026-10-11) ──────────────────────────────────────────


def leer_saldo_estimado_ia(
    session: Session, *, saldo_declarado_usd: Decimal | None, saldo_declarado_en: datetime | None
) -> Decimal | None:
    """Saldo estimado de Anthropic = el declarado menos lo gastado desde esa fecha
    (`llamadas_ia`). `None` si el usuario no declaró saldo y fecha: no se inventa
    un número (P3)."""
    if saldo_declarado_usd is None or saldo_declarado_en is None:
        return None
    return saldo_estimado_usd(saldo_declarado_usd, get_gasto_acumulado_desde(session, saldo_declarado_en))


def verificar_saldo_ia(
    session_factory: sessionmaker[Session],
    *,
    saldo_declarado_usd: Decimal | None,
    saldo_declarado_en: datetime | None,
    minimo_por_ciclo_usd: Decimal,
    now: datetime,
) -> bool:
    """`True` si se puede iniciar un ciclo automático con IA según el saldo.

    - Sin saldo declarado: `True` (no hay estimación; la guardia es opcional).
    - Saldo estimado menor que el mínimo: `False` y UN aviso por día UTC.
    - No se pudo leer el gasto: `False` (falla cerrado, igual que el tope diario).
    """
    try:
        with session_scope(session_factory) as session:
            saldo = leer_saldo_estimado_ia(
                session, saldo_declarado_usd=saldo_declarado_usd, saldo_declarado_en=saldo_declarado_en
            )
            if saldo is None:
                return True
            if not saldo_insuficiente(saldo, minimo_por_ciclo_usd):
                logger.info(
                    "saldo de IA: estimado %s USD (mínimo %s)",
                    saldo,
                    minimo_por_ciclo_usd,
                    extra={"component": _COMPONENT},
                )
                return True
            dia = inicio_del_dia_utc(now).date()
            creado = append_alerta(
                session,
                AlertaRow(
                    tipo=TipoAlerta.SALDO_IA_INSUFICIENTE,
                    subject="SISTEMA",
                    dedup_key=f"saldo-ia-insuficiente:{dia.isoformat()}",
                    occurred_at=now,
                    message=mensaje_saldo_ia_insuficiente(
                        dia, saldo_estimado=saldo, minimo_por_ciclo_usd=minimo_por_ciclo_usd
                    ),
                    observed_value=saldo,
                    threshold=minimo_por_ciclo_usd,
                ),
            )
    except Exception:  # noqa: BLE001 - falla cerrado
        logger.error(
            "saldo de IA: no se pudo estimar; el ciclo NO se inicia",
            exc_info=True,
            extra={"component": _COMPONENT},
        )
        return False
    logger.warning(
        "saldo de IA INSUFICIENTE: estimado %s USD, mínimo %s; el ciclo NO se inicia (aviso nuevo: %s)",
        saldo,
        minimo_por_ciclo_usd,
        creado,
        extra={"component": _COMPONENT},
    )
    return False
