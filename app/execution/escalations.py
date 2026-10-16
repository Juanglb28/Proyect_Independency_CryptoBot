"""Escalamientos del monitor (Paso 11a-2; sección 9 del plan).

Une las reglas puras de `app.domain.rules.escalation` con los datos guardados y
ESCRIBE filas en `alertas`. Nada más: no envía (el despachador, en
`app/notifications`, lee la tabla DESPUÉS de confirmar la transacción), no
importa agentes de IA ni `app.notifications`, y un aviso nunca cambia una
orden, una posición ni una regla de riesgo (P2). Ver la nota de D5 en
`app.domain.rules.escalation` para la única consecuencia operativa: entradas
nuevas bloqueadas mientras Binance figure caído.

Principio de diseño: AVISAR NUNCA BLOQUEA OPERAR. Cada comprobación corre en su
propio savepoint y, si falla, solo se registra en el log; el monitor sigue y
sus stops no dependen de nada de este módulo.

Idempotencia (P5): cada aviso lleva una `dedup_key` determinista (ver cada
función). Repetir el chequeo, o reiniciar a mitad, no crea avisos repetidos.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.enums import EstadoOrden, EstadoPosicion, IntencionOrden, TipoAlerta
from app.domain.rules.escalation import (
    TOLERANCIA_REFERENCIA,
    VENTANA_CAIDA,
    calcular_zscore_volumen,
    decidir_aviso_binance,
    es_volumen_atipico,
    evaluar_caida_1h,
    horas_de_referencia_volumen,
    mensaje_binance_caido,
    mensaje_binance_restablecido,
    mensaje_caida_1h,
    mensaje_discrepancia,
    mensaje_orden_pendiente,
    mensaje_pausa_drawdown,
    mensaje_volumen_atipico,
    promedio_volumen,
    ultima_vela_1h_cerrada,
)
from app.execution.reconciliation import Discrepancia
from app.execution.results_recorder import record_risk_alert
from app.repositories.alertas import AlertaRow, append_alerta, get_ultima_alerta_de_tipos
from app.repositories.candles import get_volumes_between
from app.repositories.orders import list_orders
from app.repositories.snapshots import get_hora_ultimo_snapshot, get_snapshot_de_referencia

logger = logging.getLogger(__name__)

_COMPONENT = "execution.escalations"
_SISTEMA = "SISTEMA"
_TIMEFRAME_VOLUMEN = "1h"


@dataclass(frozen=True)
class ResultadoEscalaciones:
    creadas: tuple[TipoAlerta, ...] = ()
    """Un elemento por aviso NUEVO escrito (los repetidos no cuentan)."""


def _hora_utc(now: datetime) -> str:
    return now.strftime("%Y%m%d%H")


def evaluar_escalaciones(
    session: Session,
    *,
    now: datetime,
    posiciones: Sequence,
    precios: dict[str, Decimal],
) -> ResultadoEscalaciones:
    """Corre todas las comprobaciones de un chequeo.

    `posiciones`: las posiciones que estaban ABIERTAS al empezar el chequeo (a
    las que se les pidió precio). `precios`: los precios que sí llegaron. Una
    posición que se cerró en este mismo chequeo no genera avisos de caída ni de
    volumen (ya no está abierta).
    """
    creadas: list[TipoAlerta] = []
    vigentes = [p for p in posiciones if p.status == EstadoPosicion.OPEN and p.symbol in precios]

    comprobaciones: list[tuple[str, Callable[[], list[TipoAlerta]]]] = [
        ("caída en 1 hora", lambda: _caidas(session, now, vigentes, precios)),
        ("volumen atípico", lambda: _volumenes(session, now, vigentes, precios)),
        ("órdenes pendientes", lambda: _ordenes_pendientes(session, now)),
        ("estado de Binance", lambda: _binance(session, now, posiciones, precios)),
    ]
    for nombre, comprobar in comprobaciones:
        try:
            with session.begin_nested():
                creadas.extend(comprobar())
        except Exception:  # noqa: BLE001 - avisar nunca bloquea operar
            logger.error(
                "escalaciones: falló la comprobación '%s'; el monitor sigue",
                nombre,
                exc_info=True,
                extra={"component": _COMPONENT},
            )
    return ResultadoEscalaciones(tuple(creadas))


# --- Caída en 1 hora ------------------------------------------------------------


def _caidas(session: Session, now: datetime, posiciones: Sequence, precios: dict[str, Decimal]) -> list[TipoAlerta]:
    creadas: list[TipoAlerta] = []
    limite = now - VENTANA_CAIDA
    for posicion in posiciones:
        referencia = get_snapshot_de_referencia(
            session, posicion.id, desde=limite - TOLERANCIA_REFERENCIA, hasta=limite
        )
        if referencia is None:
            continue  # sin referencia no se estima (P3)
        hora_ref, precio_ref = referencia
        caida = evaluar_caida_1h(
            precio_referencia=precio_ref,
            hora_referencia=hora_ref,
            precio_actual=precios[posicion.symbol],
            now=now,
        )
        if caida is None:
            continue
        nueva = append_alerta(
            session,
            AlertaRow(
                tipo=TipoAlerta.CAIDA_1H,
                subject=posicion.symbol,
                dedup_key=f"caida-1h:{posicion.id}:{_hora_utc(now)}",
                occurred_at=now,
                message=mensaje_caida_1h(posicion.symbol, caida),
                observed_value=caida.caida_pct,
                threshold=Decimal("5"),
                position_id=posicion.id,
            ),
        )
        if nueva:
            creadas.append(TipoAlerta.CAIDA_1H)
            record_risk_alert(session, posicion, price=precios[posicion.symbol], now=now)
    return creadas


# --- Volumen atípico ------------------------------------------------------------


def _volumenes(
    session: Session, now: datetime, posiciones: Sequence, precios: dict[str, Decimal]
) -> list[TipoAlerta]:
    creadas: list[TipoAlerta] = []
    objetivo = ultima_vela_1h_cerrada(now)
    previas = horas_de_referencia_volumen(objetivo)
    for posicion in posiciones:
        volumenes = get_volumes_between(
            session, posicion.symbol, _TIMEFRAME_VOLUMEN, open_time_from=previas[0], open_time_to=objetivo
        )
        if objetivo not in volumenes or any(h not in volumenes for h in previas):
            continue  # faltan velas: se declara dato faltante, no se estima (P3)
        referencia = [volumenes[h] for h in previas]
        zscore = calcular_zscore_volumen(volumenes[objetivo], referencia)
        if not es_volumen_atipico(zscore):
            continue
        nueva = append_alerta(
            session,
            AlertaRow(
                tipo=TipoAlerta.VOLUMEN_ATIPICO,
                subject=posicion.symbol,
                dedup_key=f"volumen:{posicion.id}:{objetivo.isoformat()}",
                occurred_at=now,
                message=mensaje_volumen_atipico(
                    posicion.symbol,
                    vela=objetivo,
                    zscore=zscore,
                    volumen=volumenes[objetivo],
                    volumen_vela_anterior=referencia[-1],
                    volumen_promedio=promedio_volumen(referencia),
                ),
                observed_value=zscore,
                threshold=Decimal("3"),
                position_id=posicion.id,
            ),
        )
        if nueva:
            creadas.append(TipoAlerta.VOLUMEN_ATIPICO)
            record_risk_alert(session, posicion, price=precios[posicion.symbol], now=now)
    return creadas


# --- Órdenes pendientes de aprobación -------------------------------------------


def _ordenes_pendientes(session: Session, now: datetime) -> list[TipoAlerta]:
    """Un aviso por cada entrada que espera aprobación manual.

    Decisión D4 (confirmada 2026-10-09): avisar de TODA entrada
    ``PENDING_APPROVAL``, no solo las de >200 USD. Si no la apruebas,
    vence a las 00:00 UTC."""
    creadas: list[TipoAlerta] = []
    pendientes = list_orders(session, estados=[EstadoOrden.PENDING_APPROVAL], intencion=IntencionOrden.ENTRY)
    for orden in pendientes:
        if orden.requested_usd is None:
            continue
        nueva = append_alerta(
            session,
            AlertaRow(
                tipo=TipoAlerta.ORDEN_PENDIENTE_APROBACION,
                subject=orden.symbol,
                dedup_key=f"orden-pendiente:{orden.id}",
                occurred_at=now,
                message=mensaje_orden_pendiente(orden.symbol, monto_usd=orden.requested_usd, vence=orden.expires_at),
                observed_value=orden.requested_usd,
                order_id=orden.id,
            ),
        )
        if nueva:
            creadas.append(TipoAlerta.ORDEN_PENDIENTE_APROBACION)
    return creadas


# --- Binance caído / restablecido -----------------------------------------------


def _binance(session: Session, now: datetime, posiciones: Sequence, precios: dict[str, Decimal]) -> list[TipoAlerta]:
    ultimo = get_ultima_alerta_de_tipos(session, [TipoAlerta.BINANCE_CAIDO, TipoAlerta.BINANCE_RESTABLECIDO])
    ultima_respuesta = (
        get_hora_ultimo_snapshot(session, [p.id for p in posiciones]) if posiciones and not precios else None
    )
    veredicto = decidir_aviso_binance(
        ultimo_aviso=ultimo.tipo if ultimo is not None else None,
        posiciones_consultadas=len(posiciones),
        precios_obtenidos=len(precios),
        ultima_respuesta=ultima_respuesta,
        now=now,
    )
    if veredicto is None:
        return []

    if veredicto == TipoAlerta.BINANCE_CAIDO:
        fila = AlertaRow(
            tipo=TipoAlerta.BINANCE_CAIDO,
            subject=_SISTEMA,
            dedup_key=f"binance-caido:{ultima_respuesta.isoformat()}",
            occurred_at=now,
            message=mensaje_binance_caido(ultima_respuesta=ultima_respuesta, now=now),
            observed_value=Decimal(int((now - ultima_respuesta).total_seconds())),
            threshold=Decimal(300),
        )
    else:
        fila = AlertaRow(
            tipo=TipoAlerta.BINANCE_RESTABLECIDO,
            subject=_SISTEMA,
            dedup_key=f"binance-restablecido:{ultimo.id}",
            occurred_at=now,
            message=mensaje_binance_restablecido(now=now),
        )
    return [veredicto] if append_alerta(session, fila) else []


# --- Avisos que no salen de la revisión por posición ------------------------------


def avisar_pausa_drawdown(
    session: Session, *, now: datetime, equity: Decimal, peak: Decimal, drawdown_pct: Decimal
) -> bool:
    """Aviso de la pausa por drawdown. Se llama una sola vez por pausa (el
    evento de pausa es de solo-agregar); la clave incluye el instante."""
    try:
        with session.begin_nested():
            return append_alerta(
                session,
                AlertaRow(
                    tipo=TipoAlerta.PAUSA_DRAWDOWN,
                    subject=_SISTEMA,
                    dedup_key=f"pausa-drawdown:{now.isoformat()}",
                    occurred_at=now,
                    message=mensaje_pausa_drawdown(equity=equity, peak=peak, drawdown_pct=drawdown_pct),
                    observed_value=drawdown_pct,
                    threshold=Decimal("-15"),
                ),
            )
    except Exception:  # noqa: BLE001 - avisar nunca bloquea operar
        logger.error("escalaciones: no se pudo guardar el aviso de pausa", exc_info=True, extra={"component": _COMPONENT})
        return False


def avisar_discrepancia(session: Session, *, now: datetime, discrepancias: Sequence[Discrepancia]) -> bool:
    """Aviso de discrepancia en la reconciliación. Se llama en una transacción
    PROPIA, después de que el chequeo se revirtió (el monitor se detiene sin
    tocar nada). Un aviso por conjunto de hallazgos y por hora UTC."""
    detalles = [f"[{d.codigo.value}] {d.detalle}" for d in discrepancias]
    huella = hashlib.sha256("\n".join(sorted(detalles)).encode("utf-8")).hexdigest()[:16]
    return append_alerta(
        session,
        AlertaRow(
            tipo=TipoAlerta.DISCREPANCIA_RECONCILIACION,
            subject=_SISTEMA,
            dedup_key=f"discrepancia:{huella}:{_hora_utc(now)}",
            occurred_at=now,
            message=mensaje_discrepancia(detalles),
            observed_value=Decimal(len(detalles)),
            threshold=Decimal(0),
        ),
    )
