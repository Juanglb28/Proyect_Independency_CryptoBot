"""Sistema: salud de datos, base de datos, monitor, reconciliación, ciclo y
costo de IA (sección 8.9). Solo lectura.

Cada dato dice cuándo fue la última vez que algo ocurrió y no interpreta de
más: "hace cuánto" se muestra como edad, y la decisión de si es mucho la toma
quien lo lee. El saldo de IA es ESTIMADO (la API de Anthropic no expone saldo).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.execution.emergency_stop import EmergencyStop
from app.execution.reconciliation import reconcile
from app.execution.system_control import get_system_state
from app.repositories.ai_calls import get_gasto_acumulado_desde, get_resumen_llamadas_desde
from app.repositories.candles import get_latest_open_time
from app.repositories.health import check_database_health
from app.services.decisions import CicloResumen, list_ciclos

SIMBOLO_DE_REFERENCIA = "BTCUSDT"
TEMPORALIDADES_DE_REFERENCIA = ("1h", "1d")
VENTANA_COSTO_IA = timedelta(days=30)


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
class SaldoIA:
    declarado_usd: Decimal
    declarado_en: datetime
    gasto_desde_entonces_usd: Decimal
    saldo_estimado_usd: Decimal


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


def get_system_view(
    session: Session,
    *,
    initial_capital: Decimal,
    now: datetime,
    saldo_declarado_usd: Decimal | None = None,
    saldo_declarado_en: datetime | None = None,
    emergency_stop: EmergencyStop | None = None,
) -> SistemaView:
    salud = check_database_health(session)
    estado = get_system_state(session, initial_capital=initial_capital, emergency_stop=emergency_stop)
    discrepancias = tuple(f"{d.codigo.value}: {d.detalle}" for d in reconcile(session))

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
    )
