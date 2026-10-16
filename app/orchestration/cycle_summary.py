"""Deja en `alertas` el resumen de un ciclo de análisis (decisión del 2026-10-11).

Ver `app.domain.rules.cycle_summary` para el texto. Aquí solo se RECOGE lo que
el ciclo ya guardó (propuestas, gasto del día) y se escribe el aviso; el envío a
Telegram lo hace el despachador después (P2: avisar nunca está en el camino de
una orden).

Nunca debe romper ni frenar un ciclo: los dos puntos de entrada atrapan
cualquier error, lo registran y devuelven `False`. Un resumen que no se pudo
escribir es una molestia; un ciclo que falla por ello sería un daño.

Idempotente: el resumen de un ciclo lleva `dedup_key = resumen-ciclo:<ciclo_id>`,
así que reintentar o relanzar el envío no lo duplica.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.domain.enums import TierEntrada, TipoAlerta
from app.domain.rules.cycle_summary import (
    DatosResumenCiclo,
    OrigenCiclo,
    PropuestaParaResumen,
    EntradaParaResumen,
    mensaje_ciclo_fallido,
    mensaje_resumen_ciclo,
)
from app.domain.rules.recovery import inicio_del_dia_utc
from app.orchestration.ai_budget import leer_saldo_estimado_ia
from app.orchestration.daily_cycle import DailyCycleResult
from app.orchestration.entry_opening import AperturaDelCiclo
from app.repositories.ai_calls import get_gasto_acumulado_desde
from app.repositories.alertas import AlertaRow, append_alerta
from app.repositories.analysis import get_propuestas_de_ciclo

logger = logging.getLogger(__name__)

_COMPONENT = "orchestration.cycle_summary"
_SISTEMA = "SISTEMA"


def construir_datos_resumen(
    session: Session,
    resultado: DailyCycleResult,
    *,
    origen: OrigenCiclo,
    tope_usd: Decimal,
    now: datetime,
    saldo_declarado_usd: Decimal | None = None,
    saldo_declarado_en: datetime | None = None,
    saldo_minimo_por_ciclo_usd: Decimal | None = None,
    apertura: AperturaDelCiclo | None = None,
    apertura_fallo: bool = False,
) -> DatosResumenCiclo:
    # Las observaciones no son propuestas de la estrategia: se resumen aparte.
    propuestas = [
        p for p in get_propuestas_de_ciclo(session, resultado.ciclo_id) if p.tier_entrada != TierEntrada.OBSERVACION
    ]
    return DatosResumenCiclo(
        origen=origen,
        ciclo_id=resultado.ciclo_id,
        terminado_en=now,
        candidatos_evaluados=resultado.candidatos_evaluados,
        candidatos_excluidos_por_calidad=resultado.candidatos_excluidos_por_calidad,
        propuestas=tuple(
            PropuestaParaResumen(
                symbol=p.symbol,
                puntaje_ponderado=p.puntaje_ponderado,
                tier_entrada=p.tier_entrada.value if p.tier_entrada is not None else None,
                confianza_global=p.confianza_global,
            )
            for p in propuestas
        ),
        costo_ciclo_usd=resultado.cost_usd,
        gasto_hoy_usd=get_gasto_acumulado_desde(session, inicio_del_dia_utc(now)),
        tope_usd=tope_usd,
        recomendacion=resultado.consolidador_recomendacion,
        razon_no_invertir=resultado.consolidador_razon_no_invertir,
        saldo_estimado_usd=leer_saldo_estimado_ia(
            session, saldo_declarado_usd=saldo_declarado_usd, saldo_declarado_en=saldo_declarado_en
        ),
        saldo_minimo_por_ciclo_usd=saldo_minimo_por_ciclo_usd,
        entradas=tuple(
            EntradaParaResumen(
                symbol=e.symbol, serie=e.serie.value, puntaje=e.puntaje, resultado=e.resultado, motivos=e.motivos
            )
            for e in (apertura.entradas if apertura is not None else ())
        ),
        entradas_informadas=apertura is not None and not apertura.bloqueada_por_reconciliacion,
        apertura_bloqueada_por_reconciliacion=apertura is not None and apertura.bloqueada_por_reconciliacion,
        apertura_fallo=apertura_fallo,
    )


def registrar_resumen_ciclo(
    session_factory: sessionmaker[Session],
    resultado: DailyCycleResult,
    *,
    origen: OrigenCiclo,
    tope_usd: Decimal,
    now: datetime,
    saldo_declarado_usd: Decimal | None = None,
    saldo_declarado_en: datetime | None = None,
    saldo_minimo_por_ciclo_usd: Decimal | None = None,
    apertura: AperturaDelCiclo | None = None,
    apertura_fallo: bool = False,
) -> bool:
    """Escribe el resumen del ciclo. `True` si quedó un aviso nuevo; `False` si
    ya existía o si algo falló (el fallo se registra y NO se propaga)."""
    try:
        with session_scope(session_factory) as session:
            datos = construir_datos_resumen(
                session,
                resultado,
                origen=origen,
                tope_usd=tope_usd,
                now=now,
                saldo_declarado_usd=saldo_declarado_usd,
                saldo_declarado_en=saldo_declarado_en,
                saldo_minimo_por_ciclo_usd=saldo_minimo_por_ciclo_usd,
                apertura=apertura,
                apertura_fallo=apertura_fallo,
            )
            return append_alerta(
                session,
                AlertaRow(
                    tipo=TipoAlerta.RESUMEN_CICLO,
                    subject=_SISTEMA,
                    dedup_key=f"resumen-ciclo:{resultado.ciclo_id}",
                    occurred_at=now,
                    message=mensaje_resumen_ciclo(datos),
                    observed_value=datos.gasto_hoy_usd,
                    threshold=datos.tope_usd,
                ),
            )
    except Exception:  # noqa: BLE001 - ver el docstring del módulo
        logger.error(
            "no se pudo guardar el resumen del ciclo",
            exc_info=True,
            extra={"component": _COMPONENT, "ciclo_id": str(resultado.ciclo_id)},
        )
        return False


def registrar_ciclo_fallido(
    session_factory: sessionmaker[Session],
    *,
    origen: OrigenCiclo,
    error: BaseException,
    now: datetime,
) -> bool:
    """Escribe el aviso de ciclo fallido (solo el NOMBRE del error). Un aviso por
    instante de falla; nunca propaga un error."""
    try:
        with session_scope(session_factory) as session:
            return append_alerta(
                session,
                AlertaRow(
                    tipo=TipoAlerta.CICLO_FALLIDO,
                    subject=_SISTEMA,
                    dedup_key=f"ciclo-fallido:{origen.value}:{now.isoformat()}",
                    occurred_at=now,
                    message=mensaje_ciclo_fallido(origen, ocurrido_en=now, tipo_error=type(error).__name__),
                ),
            )
    except Exception:  # noqa: BLE001
        logger.error(
            "no se pudo guardar el aviso de ciclo fallido",
            exc_info=True,
            extra={"component": _COMPONENT},
        )
        return False
