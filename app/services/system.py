"""Sistema: salud de datos, base de datos, monitor, reconciliación, ciclo,
costo de IA y heartbeat del scheduler (sección 8.9). Solo lectura.

Cada dato dice cuándo fue la última vez que algo ocurrió y no interpreta de
más: "hace cuánto" se muestra como edad, y la decisión de si es mucho la toma
quien lo lee. El saldo de IA es ESTIMADO (la API de Anthropic no expone saldo).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from app.domain.rules.ai_budget import presupuesto_diario_agotado
from app.domain.rules.heartbeat import UMBRAL_LATIDO_VENCIDO_MINUTOS, interpretar_latido
from app.domain.rules.recovery import inicio_del_dia_utc
from app.execution.emergency_stop import EmergencyStop
from app.execution.reconciliation import reconcile
from app.execution.system_control import get_system_state
from app.repositories.ai_calls import get_gasto_acumulado_desde, get_resumen_llamadas_desde
from app.repositories.candles import get_latest_open_time
from app.repositories.health import check_database_health
from app.services.decisions import CicloResumen, list_ciclos

logger = logging.getLogger(__name__)

SIMBOLO_DE_REFERENCIA = "BTCUSDT"
TEMPORALIDADES_DE_REFERENCIA = ("1h", "1d")
VENTANA_COSTO_IA = timedelta(days=30)

# El umbral de "latido viejo" vive en la regla de dominio
# (``app.domain.rules.heartbeat``), compartido con el vigía de Windows.

# Misma ruta que usa el scheduler (env var compartida, misma default).
_DEFAULT_HEARTBEAT_PATH = Path(os.environ.get(
    "SCHEDULER_HEARTBEAT_PATH",
    "/app/.scheduler_heartbeat.json",
))


@dataclass(frozen=True)
class FrescuraDeVelas:
    symbol: str
    timeframe: str
    ultima_vela: datetime | None
    edad: timedelta | None


@dataclass(frozen=True)
class CostoIA:
    desde: datetime
    llamadas: int
    fallidas: int
    costo_usd: Decimal


@dataclass(frozen=True)
class PresupuestoIAHoy:
    """Gasto de IA del día UTC frente al tope diario de los ciclos automáticos."""

    gasto_hoy_usd: Decimal
    tope_usd: Decimal
    agotado: bool


@dataclass(frozen=True)
class SaldoIA:
    declarado_usd: Decimal
    declarado_en: datetime
    gasto_desde_entonces_usd: Decimal
    saldo_estimado_usd: Decimal


@dataclass(frozen=True)
class HeartbeatStatus:
    """Estado del heartbeat del scheduler, leído del archivo JSON."""

    disponible: bool
    """``True`` si se pudo leer el archivo."""
    timestamp: datetime | None
    """Última vez que el scheduler escribió el heartbeat."""
    pid: int | None
    """PID del proceso scheduler."""
    edad: timedelta | None
    """Antigüedad del heartbeat (``now - timestamp``)."""
    stale: bool
    """``True`` si el heartbeat es demasiado viejo (> 15 min)."""


@dataclass(frozen=True)
class SistemaView:
    generado_en: datetime
    base_de_datos_ok: bool
    timescaledb_ok: bool
    detalle_base_de_datos: str
    sistema_pausado: bool
    parada_emergencia: bool
    posiciones_abiertas: int
    equity_actual: Decimal | None
    pico_equity: Decimal
    ultima_observacion_monitor: datetime | None
    edad_monitor: timedelta | None
    discrepancias: tuple[str, ...]
    velas: tuple[FrescuraDeVelas, ...]
    ciclos_recientes: tuple[CicloResumen, ...]
    costo_ia: CostoIA
    saldo_ia: SaldoIA | None
    heartbeat: HeartbeatStatus
    presupuesto_ia: PresupuestoIAHoy | None = None


def read_heartbeat(heartbeat_path: Path, *, now: datetime) -> HeartbeatStatus:
    """Lee el archivo de heartbeat del scheduler y devuelve su estado.

    Si el archivo no existe, no se puede leer o tiene formato inválido,
    devuelve ``HeartbeatStatus(disponible=False, ...)``.
    """
    try:
        raw: str | None = heartbeat_path.read_text(encoding="utf-8")
    except OSError:
        raw = None
    lectura = interpretar_latido(raw, now=now, umbral_minutos=UMBRAL_LATIDO_VENCIDO_MINUTOS)
    return HeartbeatStatus(
        disponible=lectura.disponible,
        timestamp=lectura.timestamp,
        pid=lectura.pid,
        edad=lectura.edad,
        stale=lectura.vencido,
    )


def get_system_view(
    session: Session,
    *,
    initial_capital: Decimal,
    now: datetime,
    saldo_declarado_usd: Decimal | None = None,
    saldo_declarado_en: datetime | None = None,
    emergency_stop: EmergencyStop | None = None,
    heartbeat_path: Path | None = None,
    tope_ia_diario_usd: Decimal | None = None,
) -> SistemaView:
    salud = check_database_health(session)
    estado = get_system_state(session, initial_capital=initial_capital, emergency_stop=emergency_stop)
    discrepancias = tuple(f"{d.codigo.value}: {d.detalle}" for d in reconcile(session))
    hb_path = heartbeat_path if heartbeat_path is not None else _DEFAULT_HEARTBEAT_PATH
    heartbeat = read_heartbeat(hb_path, now=now)

    velas = []
    for timeframe in TEMPORALIDADES_DE_REFERENCIA:
        ultima = get_latest_open_time(session, SIMBOLO_DE_REFERENCIA, timeframe)
        velas.append(FrescuraDeVelas(SIMBOLO_DE_REFERENCIA, timeframe, ultima, None if ultima is None else now - ultima))

    desde = now - VENTANA_COSTO_IA
    llamadas, fallidas, costo = get_resumen_llamadas_desde(session, desde)

    saldo = None
    if saldo_declarado_usd is not None and saldo_declarado_en is not None:
        gasto = get_gasto_acumulado_desde(session, saldo_declarado_en)
        saldo = SaldoIA(saldo_declarado_usd, saldo_declarado_en, gasto, saldo_declarado_usd - gasto)

    presupuesto = None
    if tope_ia_diario_usd is not None:
        gasto_hoy = get_gasto_acumulado_desde(session, inicio_del_dia_utc(now))
        presupuesto = PresupuestoIAHoy(
            gasto_hoy, tope_ia_diario_usd, presupuesto_diario_agotado(gasto_hoy, tope_ia_diario_usd)
        )

    return SistemaView(
        generado_en=now,
        base_de_datos_ok=salud.database_reachable,
        timescaledb_ok=salud.timescaledb_installed,
        detalle_base_de_datos=salud.detail,
        sistema_pausado=estado.pausado,
        parada_emergencia=estado.parada_emergencia,
        posiciones_abiertas=estado.posiciones_abiertas,
        equity_actual=estado.ultimo_equity,
        pico_equity=estado.pico_vigente,
        ultima_observacion_monitor=estado.ultima_observacion_en,
        edad_monitor=None if estado.ultima_observacion_en is None else now - estado.ultima_observacion_en,
        discrepancias=discrepancias,
        velas=tuple(velas),
        ciclos_recientes=list_ciclos(session, limit=10),
        costo_ia=CostoIA(desde, llamadas, fallidas, costo),
        saldo_ia=saldo,
        heartbeat=heartbeat,
        presupuesto_ia=presupuesto,
    )
