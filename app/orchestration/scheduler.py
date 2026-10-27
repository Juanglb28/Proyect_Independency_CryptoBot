"""Proceso scheduler: APScheduler 3.x con BlockingScheduler (Paso 11b).

Orquesta los cinco jobs periódicos del sistema (más una recuperación única de
velas vencidas al arrancar, ver abajo):

1. **Monitor** — cada 60 s: reconcilia estado, vigila posiciones abiertas,
   ejecuta stop / TP / trailing, calcula equity, registra drawdown. Python
   determinista, sin IA (P2). Si la reconciliación detecta discrepancias
   (``ReconciliationBlocked``) registra la alerta y sigue (no mata al
   scheduler — lo que hace ``scripts/monitor.py`` en modo ``--loop``).

2. **Ciclo horario** — al minuto 5 de cada hora: ingesta incremental de
   velas para las temporalidades que cierran a esa hora, recalcula
   indicadores, corre el pre-filtro determinista y detecta candidatos
   NUEVOS sobre umbral 45 (enmienda E3). Si hay nuevos, dispara un ciclo
   diario IA (``run_daily_cycle``).

3. **Ciclo diario** — a las 00:05 UTC: backfill de todas las
   temporalidades (ventana de 7 días), indicadores, y ``run_daily_cycle``
   (pipeline IA completo). A las 00:00 UTC cierra la vela diaria; 5 min de
   margen es suficiente para que Binance publique la vela definitiva.

4. **Refresco de velas** — a las 14:30 UTC (9:30 a. m. en Colombia): repasa las
   velas de 1h/4h/12h e indicadores, por si el equipo estaba apagado a las 12:05
   UTC y la vela de 12 h quedó a medias. Solo velas e indicadores: sin pre-filtro,
   sin candidatos nuevos, sin IA (2026-10-17).

5. **Heartbeat** — cada 5 min: escribe un JSON en ``/app/`` (la raíz del
   contenedor Docker) con ``status``, ``timestamp`` y ``pid``. La UI lee
   este archivo para mostrar si el scheduler está vivo.

Recuperación de velas vencidas (2026-10-17): unos segundos después de arrancar, un
job de una sola ejecución (``stale_candle_catchup``) repara las velas guardadas a
medias cuya hora de cierre ya pasó (equipo apagado o dormido en un cierre). Solo
velas e indicadores; corre en un hilo aparte y no retrasa al monitor.

Arranque y recuperación (Paso 11c)
----------------------------------
Antes de registrar los jobs: (1) se toma un candado de proceso único en
PostgreSQL (``app.repositories.process_lock``); si otro scheduler ya vive, este
sale con código 1 en vez de duplicar ciclos; (2) ``run_startup_recovery``
marca como interrumpidos los ciclos que quedaron a medias, reconcilia órdenes y
posiciones y avisa si hoy falta el ciclo diario (NO lo relanza: decisión del
2026-10-10); (3) se despachan los avisos resultantes.

Decisiones implementadas
-------------------------
- E3 (aprobada 2026-10-09): ingesta horaria + pre-filtro determinista, IA
  solo para candidatos nuevos.
- D4 (confirmada 2026-10-09): el despacho de alertas (incluyendo entradas
  pendientes) lo hace el dispatcher después de cada tick del monitor. Este
  módulo no filtra — llama a ``dispatch_pending_alerts`` que envía TODO lo
  pendiente.
- Railway data copy: la copia es ONE-TIME (destino vacío obligatorio);
  no se puede automatizar como job periódico con el script actual. Se deja
  un TODO para implementar copia incremental (task #68).

Principios respetados
----------------------
- P1: el scheduler no calcula nada financiero — llama a funciones
  deterministas que ya existen.
- P2: la IA nunca está en el camino del monitor ni de la ejecución.
- P4: el scheduler NO importa de ``scripts/`` (dependencia prohibida
  app → scripts). Usa directamente ``app.execution.monitor.run_monitor_tick``.
- P5: todo el estado vive en PostgreSQL. Si el scheduler muere y revive,
  la base de datos contiene todo lo necesario para continuar.

Uso
----
Como proceso de larga duración dentro de Docker::

    docker compose up scheduler

O localmente::

    python -m app.orchestration.scheduler
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from apscheduler.events import EVENT_JOB_ERROR, JobExecutionEvent
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.data.candle_ingestion import backfill_all
from app.data.exchange_client import SUPPORTED_TIMEFRAMES
from app.data.indicator_pipeline import calculate_indicators_for_all
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.rules.recovery import HORA_CICLO_DIARIO_UTC
from app.domain.symbols import ALLOWED_SYMBOLS
from app.execution.escalations import avisar_discrepancia
from app.execution.monitor import ReconciliationBlocked, run_monitor_tick
from app.execution.price_source import BinanceTickerPriceSource, PriceSource
from app.logging_config import configure_logging
from app.notifications.deadman import ping_deadman
from app.notifications.dispatcher import dispatch_pending_alerts
from app.notifications.telegram import Sender, build_sender
from app.domain.rules.cycle_summary import OrigenCiclo
from app.orchestration.ai_budget import verificar_presupuesto_ia, verificar_saldo_ia
from app.orchestration.cycle_summary import registrar_ciclo_fallido, registrar_resumen_ciclo
from app.orchestration.daily_cycle import run_daily_cycle
from app.orchestration.entry_opening import abrir_entradas_del_ciclo
from app.orchestration.hourly_cycle import (
    REFRESCO_VELAS_HORA_UTC,
    run_candle_refresh,
    run_hourly_cycle,
    run_stale_candle_catchup,
    timeframes_que_cierran_a_esta_hora,
)
from app.orchestration.lifecycle import run_startup_recovery
from app.repositories.process_lock import SchedulerLock

logger = logging.getLogger(__name__)

_COMPONENT = "orchestration.scheduler"

# ── Intervalos y ventanas ──────────────────────────────────────────────

MONITOR_INTERVAL_SECONDS = 60
HEARTBEAT_INTERVAL_MINUTES = 5
DAILY_BACKFILL_WINDOW_DAYS = 7
REFRESCO_VELAS_GRACIA_SEGUNDOS = 3 * 3600
"""Si el equipo estaba dormido a las 14:30 UTC y despierta hasta 3 h después, el
refresco corre una vez al despertar en vez de perderse hasta mañana (es idempotente)."""
CICLO_DIARIO_GRACIA_SEGUNDOS = 4 * 3600
"""Margen de recuperación del ciclo diario de las 00:05 UTC (decisión del usuario, 2026-10-26).
APScheduler descarta un job cuya hora ya pasó por más de ``misfire_grace_time`` (por defecto,
1 s). El 26/10 el equipo despertó de la suspensión 21 s antes de las 00:05 UTC y el ciclo no
se lanzó, sin dejar alerta (causa probable: llegó tarde por más de ese segundo; no quedó
registro que lo confirme). Con 4 h, si el equipo
duerme a las 00:05 y despierta hasta 4 h después, el ciclo corre UNA vez al despertar
(``coalesce=True``). Pasado ese margen sigue sin recuperarse: lo cubre el aviso de ciclo perdido."""
RECUPERACION_VELAS_RETRASO_SEGUNDOS = 10
RECUPERACION_VELAS_GRACIA_SEGUNDOS = 600
"""La recuperación de velas vencidas corre una vez, unos segundos después de arrancar
(en un hilo aparte del scheduler: no retrasa al monitor)."""

# Ruta del heartbeat: dentro de /app/ (raíz del contenedor Docker, donde
# el volumen .:/app monta el proyecto). En desarrollo local, queda en la
# raíz del proyecto.
HEARTBEAT_PATH = Path(os.environ.get(
    "SCHEDULER_HEARTBEAT_PATH",
    "/app/.scheduler_heartbeat.json",
))

# ── Lock para evitar ciclos diarios concurrentes ───────────────────────
# El ciclo horario puede disparar un ciclo diario IA cuando detecta
# candidatos nuevos; si coincide con el ciclo diario programado a las
# 00:05, solo uno debe correr. ``threading.Lock`` basta porque
# APScheduler 3.x usa threads (no procesos).
_daily_cycle_lock = threading.Lock()


# ── Estado compartido (inicializado una vez en ``start_scheduler``) ────

class _SharedState:
    """Recursos compartidos entre los jobs del scheduler.

    Se inicializan una sola vez en ``start_scheduler()`` y se pasan a los
    jobs via ``args`` de APScheduler. Esto evita crear un engine / cliente
    por cada tick (lo que agotaría conexiones y rate-limits).
    """

    __slots__ = (
        "session_factory",
        "settings",
        "price_source",
        "sender",
    )

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        price_source: PriceSource,
        sender: Sender | None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.price_source = price_source
        self.sender = sender


# ═══════════════════════════════════════════════════════════════════════
# Job 1: Monitor (cada 60 s)
# ═══════════════════════════════════════════════════════════════════════


def _job_monitor(state: _SharedState) -> None:
    """Un tick del monitor en su propia transacción.

    Patrón copiado de ``scripts/monitor.run_once`` pero sin la capa de
    presentación (el scheduler usa logging estructurado, no stdout).
    Si la reconciliación falla, registra la discrepancia y continúa — el
    scheduler NO debe morir por una discrepancia (P5).
    """
    now = datetime.now(timezone.utc)
    try:
        with session_scope(state.session_factory) as session:
            result = run_monitor_tick(
                session,
                now=now,
                price_source=state.price_source,
                initial_capital=state.settings.paper_capital_usd,
            )
    except ReconciliationBlocked as exc:
        # Registrar el aviso en transacción aparte (la del tick se revirtió)
        logger.critical(
            "Monitor: reconciliación bloqueada — %d discrepancia(s)",
            len(exc.discrepancias),
            extra={"component": _COMPONENT},
        )
        try:
            with session_scope(state.session_factory) as session:
                avisar_discrepancia(session, now=now, discrepancias=exc.discrepancias)
        except Exception:
            logger.error(
                "Monitor: no se pudo guardar el aviso de discrepancia",
                exc_info=True,
                extra={"component": _COMPONENT},
            )
        # Despachar alertas pendientes (incluyendo la discrepancia recién guardada)
        _despachar_alertas(state, now)
        return
    except Exception:
        logger.error(
            "Monitor: el chequeo falló; se reintenta en el siguiente tick",
            exc_info=True,
            extra={"component": _COMPONENT},
        )
        return

    # Log mínimo del resultado
    if result.equity is not None:
        logger.info(
            "Monitor: equity=%.2f USD  drawdown=%.2f%%  abiertas=%d",
            result.equity,
            result.drawdown_pct or Decimal(0),
            result.posiciones_abiertas,
            extra={"component": _COMPONENT},
        )
    if result.hubo_problemas:
        logger.warning(
            "Monitor: hubo problemas (sin precio o errores de posición)",
            extra={"component": _COMPONENT},
        )

    # Despachar TODAS las alertas pendientes (D4: incluye entradas pendientes)
    _despachar_alertas(state, now)


# ═══════════════════════════════════════════════════════════════════════
# Job 2: Ciclo horario (minuto 5 de cada hora)
# ═══════════════════════════════════════════════════════════════════════


def _job_hourly(state: _SharedState) -> None:
    """Ingesta incremental, indicadores, pre-filtro y detección de
    candidatos nuevos (E3).

    Si hay candidatos nuevos sobre umbral 45, dispara un ciclo diario IA.
    A la hora 0 UTC se salta: el ciclo diario programado a las 00:05 ya
    cubre todo (ingesta completa + indicadores + IA).
    """
    now = datetime.now(timezone.utc)
    hora_utc = now.hour

    # A las 00:xx el ciclo diario programado hace todo — el horario sobra
    if hora_utc == 0:
        logger.info(
            "Ciclo horario: hora 0 UTC — se salta (el ciclo diario lo cubre)",
            extra={"component": _COMPONENT, "hora_utc": hora_utc},
        )
        return

    try:
        with session_scope(state.session_factory) as session:
            result = run_hourly_cycle(session, state.settings, now=now)
    except Exception:
        logger.error(
            "Ciclo horario: falló",
            exc_info=True,
            extra={"component": _COMPONENT, "hora_utc": hora_utc},
        )
        return

    logger.info(
        "Ciclo horario completado: hora=%d, temporalidades=%s, "
        "nuevos_candidatos=%d, elapsed=%.1fs",
        result.hora_utc,
        result.timeframes_ingested,
        len(result.new_candidates),
        result.elapsed_seconds,
        extra={
            "component": _COMPONENT,
            "hora_utc": result.hora_utc,
            "nuevos": [c.symbol for c in result.new_candidates],
        },
    )

    # Si hay candidatos nuevos → disparar ciclo diario IA
    if result.new_candidates:
        logger.info(
            "Candidatos nuevos detectados (%s) — disparando ciclo diario IA",
            [c.symbol for c in result.new_candidates],
            extra={"component": _COMPONENT},
        )
        _ejecutar_ciclo_diario(state, trigger="hourly_new_candidates")


# ═══════════════════════════════════════════════════════════════════════
# Job 4: Refresco de velas (14:30 UTC = 9:30 a. m. Colombia)
# ═══════════════════════════════════════════════════════════════════════


def _trigger_refresco_velas() -> CronTrigger:
    """Disparador diario del refresco: ``REFRESCO_VELAS_HORA_UTC`` (UTC)."""
    return CronTrigger(hour=REFRESCO_VELAS_HORA_UTC.hour, minute=REFRESCO_VELAS_HORA_UTC.minute, timezone="UTC")


def _job_refresco_velas(state: _SharedState) -> None:
    """Repasa las velas 1h/4h/12h y sus indicadores (solo datos derivados).

    Existe porque el ciclo horario de las 12:05 UTC (7:05 a. m. en Colombia) se
    pierde si el equipo está apagado y la vela de 12 h queda a medias. No corre el
    pre-filtro ni dispara IA. Un fallo se registra y no mata al scheduler.
    """
    now = datetime.now(timezone.utc)
    try:
        with session_scope(state.session_factory) as session:
            result = run_candle_refresh(session, now=now)
    except Exception:
        logger.error(
            "Refresco de velas: falló; el ciclo diario de las 00:05 UTC vuelve a cubrirlo",
            exc_info=True,
            extra={"component": _COMPONENT},
        )
        return

    logger.info(
        "Refresco de velas completado: temporalidades=%s, nuevas=%d, actualizadas=%d, indicadores=%d, elapsed=%.1fs",
        result.timeframes,
        result.velas_nuevas,
        result.velas_actualizadas,
        result.indicadores_nuevos,
        result.elapsed_seconds,
        extra={"component": _COMPONENT},
    )


def _job_recuperar_velas_vencidas(state: _SharedState) -> None:
    """Al arrancar: repara las velas guardadas a medias cuya hora de cierre ya pasó.

    Cubre el caso de un equipo que estuvo apagado o dormido y se perdió el cierre de
    una vela (p. ej. la de 12 h a las 12:00 UTC). Se ejecuta una sola vez, unos
    segundos después de arrancar; solo velas e indicadores (sin pre-filtro ni IA).
    Un fallo se registra y no mata al scheduler.
    """
    try:
        result = run_stale_candle_catchup(state.session_factory, now=datetime.now(timezone.utc))
    except Exception:
        logger.error(
            "Recuperación de velas vencidas: falló; el refresco de las 14:30 UTC y el ciclo diario vuelven a cubrirlo",
            exc_info=True,
            extra={"component": _COMPONENT},
        )
        return

    if result.series_con_velas_vencidas == 0:
        logger.info(
            "Recuperación de velas vencidas: no hay velas vencidas sin cerrar",
            extra={"component": _COMPONENT},
        )
        return
    log = logger.warning if (result.series_fallidas or result.velas_sin_reparar) else logger.info
    log(
        "Recuperación de velas vencidas: series=%d, velas reparadas=%d, series fallidas=%d, "
        "velas sin reparar=%d, elapsed=%.1fs",
        result.series_con_velas_vencidas,
        result.velas_reparadas,
        result.series_fallidas,
        result.velas_sin_reparar,
        result.elapsed_seconds,
        extra={"component": _COMPONENT},
    )


# ═══════════════════════════════════════════════════════════════════════
# Job 3: Ciclo diario (00:05 UTC)
# ═══════════════════════════════════════════════════════════════════════


def _job_daily(state: _SharedState) -> None:
    """Backfill completo + indicadores + ciclo diario IA (pipeline de
    análisis con agentes).

    El backfill diario cubre 7 días hacia atrás para todas las
    temporalidades — cubre huecos de caídas de hasta una semana sin
    repetir el backfill histórico de 2 años.
    """
    _ejecutar_ciclo_diario(state, trigger="scheduled_daily_0005")


def _origen_del_trigger(trigger: str) -> OrigenCiclo:
    return OrigenCiclo.CANDIDATOS_NUEVOS if trigger == "hourly_new_candidates" else OrigenCiclo.PROGRAMADO_DIARIO


def _ejecutar_ciclo_diario(state: _SharedState, *, trigger: str) -> None:
    """Núcleo compartido del ciclo diario: lo usa tanto el job programado
    a las 00:05 como el ciclo horario cuando detecta candidatos nuevos.

    El ``_daily_cycle_lock`` impide que dos ejecuciones corran al mismo
    tiempo (el horario detecta nuevos mientras el diario está corriendo).
    """
    acquired = _daily_cycle_lock.acquire(blocking=False)
    if not acquired:
        logger.warning(
            "Ciclo diario: ya hay uno en curso — se salta (trigger=%s)",
            trigger,
            extra={"component": _COMPONENT},
        )
        return

    try:
        now = datetime.now(timezone.utc)

        # Tope diario de gasto de IA (decisión del 2026-10-10): antes de
        # tocar nada. Si está agotado, o no se puede leer el gasto, no se
        # inicia el ciclo (el aviso ya quedó guardado por la verificación).
        if not verificar_presupuesto_ia(
            state.session_factory, tope_usd=state.settings.ai_daily_budget_usd, now=now
        ):
            _despachar_alertas(state, now)
            logger.warning(
                "Ciclo diario: NO se inicia por el tope diario de IA (trigger=%s)",
                trigger,
                extra={"component": _COMPONENT, "trigger": trigger},
            )
            return

        # Guardia de saldo (2026-10-11): sin crédito suficiente el ciclo se
        # quedaría a mitad de camino y se cobraría lo gastado.
        if not verificar_saldo_ia(
            state.session_factory,
            saldo_declarado_usd=state.settings.anthropic_saldo_declarado_usd,
            saldo_declarado_en=state.settings.anthropic_saldo_declarado_en,
            minimo_por_ciclo_usd=state.settings.ai_min_balance_per_cycle_usd,
            now=now,
        ):
            _despachar_alertas(state, now)
            logger.warning(
                "Ciclo diario: NO se inicia por saldo de IA insuficiente (trigger=%s)",
                trigger,
                extra={"component": _COMPONENT, "trigger": trigger},
            )
            return

        logger.info(
            "Ciclo diario: INICIO (trigger=%s)",
            trigger,
            extra={"component": _COMPONENT, "trigger": trigger},
        )

        # Fase 1: backfill de todas las temporalidades (ventana 7 días)
        since = now - timedelta(days=DAILY_BACKFILL_WINDOW_DAYS)
        with session_scope(state.session_factory) as session:
            backfill_results = backfill_all(
                session,
                ALLOWED_SYMBOLS,
                SUPPORTED_TIMEFRAMES,
                since,
                until=now,
                now=now,
            )
        total_inserted = sum(r.inserted for r in backfill_results)
        total_updated = sum(r.updated for r in backfill_results)
        logger.info(
            "Ciclo diario: backfill — %d velas nuevas, %d actualizadas",
            total_inserted,
            total_updated,
            extra={"component": _COMPONENT},
        )

        # Fase 2: indicadores para todas las temporalidades
        with session_scope(state.session_factory) as session:
            indicator_results = calculate_indicators_for_all(
                session, ALLOWED_SYMBOLS, SUPPORTED_TIMEFRAMES
            )
        total_indicators = sum(r.inserted for r in indicator_results)
        logger.info(
            "Ciclo diario: indicadores — %d nuevos calculados",
            total_indicators,
            extra={"component": _COMPONENT},
        )

        # Fase 3: pipeline IA completo (run_daily_cycle gestiona sus
        # propios commits y la persistencia del prefiltro/ciclo)
        with session_scope(state.session_factory) as session:
            daily_result = run_daily_cycle(session, state.settings, now=now)

        logger.info(
            "Ciclo diario: FIN (trigger=%s) — ciclo_id=%s, "
            "candidatos=%d, propuestas=%d, cost=%.4f USD",
            trigger,
            daily_result.ciclo_id,
            len(daily_result.candidatos_evaluados),
            len(daily_result.propuesta_ids),
            daily_result.cost_usd,
            extra={
                "component": _COMPONENT,
                "trigger": trigger,
                "ciclo_id": str(daily_result.ciclo_id),
            },
        )

        # Fase 4: apertura AUTOMÁTICA de posiciones paper (2026-10-12): las de la
        # estrategia que calificaron + las de observación (candidatos del
        # pre-filtro). Código determinista, sin IA (P2). Si falla, el ciclo de
        # análisis ya quedó guardado: se registra, se avisa en el resumen y NO
        # se hace fallar el ciclo.
        apertura = None
        apertura_fallo = False
        try:
            with session_scope(state.session_factory) as session:
                apertura = abrir_entradas_del_ciclo(
                    session,
                    daily_result.ciclo_id,
                    now=datetime.now(timezone.utc),
                    price_source=state.price_source,
                )
        except Exception:  # noqa: BLE001 - ver el comentario de arriba
            apertura_fallo = True
            logger.error(
                "Ciclo diario: la apertura de posiciones paper FALLÓ (trigger=%s)",
                trigger,
                exc_info=True,
                extra={"component": _COMPONENT, "trigger": trigger, "ciclo_id": str(daily_result.ciclo_id)},
            )

        # Resumen del ciclo para Telegram (2026-10-11); nunca frena el ciclo
        registrar_resumen_ciclo(
            state.session_factory,
            daily_result,
            origen=_origen_del_trigger(trigger),
            tope_usd=state.settings.ai_daily_budget_usd,
            now=datetime.now(timezone.utc),
            saldo_declarado_usd=state.settings.anthropic_saldo_declarado_usd,
            saldo_declarado_en=state.settings.anthropic_saldo_declarado_en,
            saldo_minimo_por_ciclo_usd=state.settings.ai_min_balance_per_cycle_usd,
            apertura=apertura,
            apertura_fallo=apertura_fallo,
        )

        # Despachar alertas generadas por el ciclo (si las hay)
        _despachar_alertas(state, now)

    except Exception as exc:
        logger.error(
            "Ciclo diario: FALLÓ (trigger=%s)",
            trigger,
            exc_info=True,
            extra={"component": _COMPONENT, "trigger": trigger},
        )
        ahora = datetime.now(timezone.utc)
        registrar_ciclo_fallido(
            state.session_factory, origen=_origen_del_trigger(trigger), error=exc, now=ahora
        )
        _despachar_alertas(state, ahora)
    finally:
        _daily_cycle_lock.release()


# ═══════════════════════════════════════════════════════════════════════
# Job 4: Heartbeat (cada 5 min)
# ═══════════════════════════════════════════════════════════════════════


def _job_heartbeat(deadman_url: str | None = None) -> None:
    """Escribe un JSON mínimo para que la UI (y el vigía local de Windows) sepan
    que el scheduler vive, y, si está configurado, pega al dead-man's-switch
    externo.

    El archivo se escribe con rename atómico para evitar lecturas parciales. Si
    algo falla (permisos, disco lleno, red) solo se logea — el latido es
    informativo y nunca debe tumbar al scheduler (P5). El ping externo es
    best-effort e independiente de que el archivo se haya podido escribir.
    """
    payload = {
        "status": "alive",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
    }
    tmp = HEARTBEAT_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(HEARTBEAT_PATH)
    except Exception:
        logger.warning(
            "Heartbeat: no se pudo escribir %s",
            HEARTBEAT_PATH,
            exc_info=True,
            extra={"component": _COMPONENT},
        )
    # Dead-man's-switch externo (nunca lanza; nunca registra la URL).
    ping_deadman(deadman_url)


# ═══════════════════════════════════════════════════════════════════════
# Despacho de alertas (compartido)
# ═══════════════════════════════════════════════════════════════════════


def _despachar_alertas(state: _SharedState, now: datetime) -> None:
    """Envía TODAS las alertas pendientes por Telegram (D4). Nunca propaga
    un fallo — Telegram no puede detener al scheduler."""
    try:
        despacho = dispatch_pending_alerts(
            state.session_factory, state.sender, now=now
        )
        if despacho.enviadas:
            logger.info(
                "Alertas despachadas: %d enviadas, %d fallidas",
                despacho.enviadas,
                despacho.fallidas,
                extra={"component": _COMPONENT},
            )
    except Exception:
        logger.error(
            "Despacho de alertas falló",
            exc_info=True,
            extra={"component": _COMPONENT},
        )


# ═══════════════════════════════════════════════════════════════════════
# Listener de errores (red de seguridad)
# ═══════════════════════════════════════════════════════════════════════


def _on_job_error(event: JobExecutionEvent) -> None:
    """Red de seguridad: si un job propaga una excepción que no debería
    haber pasado (todos los jobs tienen try/except propio), al menos queda
    en el log. APScheduler 3.x no mata al scheduler por un job fallido
    (behavior de ``max_instances=1, coalesce=True``), así que esto es
    puramente informativo."""
    logger.error(
        "APScheduler: job %s falló con excepción no capturada",
        event.job_id,
        exc_info=event.exception,
        extra={"component": _COMPONENT, "job_id": event.job_id},
    )


# ═══════════════════════════════════════════════════════════════════════
# Check de conectividad a la base (informativo al arranque)
# ═══════════════════════════════════════════════════════════════════════


def _check_db(session_factory: sessionmaker[Session]) -> bool:
    """Intenta una consulta trivial. Devuelve ``True`` si la base responde.

    Es informativo: si falla, el scheduler arranca igual (la base puede
    tardar unos segundos más en estar lista dentro de Docker y los jobs
    reintentan solos).
    """
    try:
        from sqlalchemy import text

        with session_scope(session_factory) as session:
            session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════
# Punto de entrada
# ═══════════════════════════════════════════════════════════════════════


def start_scheduler() -> None:
    """Arranca el scheduler con los cuatro jobs. Bloquea hasta SIGINT/SIGTERM.

    Este es el punto de entrada principal del proceso scheduler. Inicializa
    los recursos compartidos una sola vez y los pasa a los jobs como
    argumentos (evita globals mutables).
    """
    configure_logging()
    logger.info(
        "Scheduler: iniciando",
        extra={"component": _COMPONENT, "pid": os.getpid()},
    )

    # ── Recursos compartidos ──
    settings = get_settings()
    engine = get_engine(settings)
    session_factory = get_sessionmaker(engine)

    # ── Proceso único (Paso 11c) ──
    # Sin este candado dos schedulers duplicarían ciclos y gasto de IA. Si no
    # se puede ni consultar la base, tampoco se puede garantizar que seamos
    # el único: se sale y Docker (`restart: unless-stopped`) reintenta.
    process_lock = SchedulerLock(engine)
    try:
        lock_obtenido = process_lock.try_acquire()
    except Exception:
        logger.critical(
            "Scheduler: no se pudo verificar el candado de proceso único (¿base caída?); saliendo",
            exc_info=True,
            extra={"component": _COMPONENT},
        )
        sys.exit(1)
    if not lock_obtenido:
        logger.critical(
            "Scheduler: ya hay otro scheduler corriendo contra esta base; saliendo para no duplicar ciclos",
            extra={"component": _COMPONENT},
        )
        sys.exit(1)

    price_source = BinanceTickerPriceSource()
    sender = build_sender(
        settings.telegram_bot_token, settings.telegram_chat_id
    )
    if sender is None:
        logger.warning(
            "Telegram sin configurar: los avisos se guardan en la base y "
            "se enviarán cuando lo configures",
            extra={"component": _COMPONENT},
        )

    state = _SharedState(session_factory, settings, price_source, sender)

    # ── Check de DB (informativo) ──
    if _check_db(session_factory):
        logger.info(
            "Scheduler: conectividad con la base OK",
            extra={"component": _COMPONENT},
        )
    else:
        logger.warning(
            "Scheduler: la base no responde al arranque — los jobs "
            "reintentarán por su cuenta",
            extra={"component": _COMPONENT},
        )

    # ── Recuperación al arrancar (Paso 11c) ──
    inicio = datetime.now(timezone.utc)
    reporte = run_startup_recovery(session_factory, now=inicio)
    if not reporte.estado_sano:
        logger.critical(
            "Scheduler: arranque con el estado sin validar (discrepancias=%d, errores=%d); "
            "el monitor y las entradas siguen bloqueados por reconciliación hasta resolverlo",
            reporte.discrepancias,
            len(reporte.errores),
            extra={"component": _COMPONENT},
        )
    _despachar_alertas(state, inicio)

    # ── Heartbeat inicial (incluye el primer ping al dead-man's-switch) ──
    _job_heartbeat(settings.scheduler_deadman_url)

    # ── Configurar APScheduler ──
    scheduler = BlockingScheduler(
        timezone="UTC",
        job_defaults={
            "max_instances": 1,
            "coalesce": True,
        },
    )

    # Job 1: Monitor — cada 60 s
    scheduler.add_job(
        _job_monitor,
        trigger=IntervalTrigger(seconds=MONITOR_INTERVAL_SECONDS),
        args=[state],
        id="monitor",
        name="Monitor de posiciones (cada 60s)",
    )

    # Job 2: Ciclo horario — al minuto 5 de cada hora
    scheduler.add_job(
        _job_hourly,
        trigger=CronTrigger(minute=5),
        args=[state],
        id="hourly_cycle",
        name="Ciclo horario (ingesta + detección, minuto 5)",
    )

    # Job 3: Ciclo diario — 00:05 UTC
    scheduler.add_job(
        _job_daily,
        trigger=CronTrigger(hour=HORA_CICLO_DIARIO_UTC.hour, minute=HORA_CICLO_DIARIO_UTC.minute),
        args=[state],
        id="daily_cycle",
        name="Ciclo diario (backfill + IA, 00:05 UTC)",
        misfire_grace_time=CICLO_DIARIO_GRACIA_SEGUNDOS,
    )

    # Job 6: Recuperación de velas vencidas — una sola vez, poco después de arrancar
    scheduler.add_job(
        _job_recuperar_velas_vencidas,
        trigger=DateTrigger(
            run_date=datetime.now(timezone.utc) + timedelta(seconds=RECUPERACION_VELAS_RETRASO_SEGUNDOS),
            timezone="UTC",
        ),
        args=[state],
        id="stale_candle_catchup",
        name="Recuperación de velas vencidas (una vez al arrancar)",
        misfire_grace_time=RECUPERACION_VELAS_GRACIA_SEGUNDOS,
    )

    # Job 5: Refresco de velas — 14:30 UTC (9:30 a. m. Colombia). Solo velas e indicadores.
    scheduler.add_job(
        _job_refresco_velas,
        trigger=_trigger_refresco_velas(),
        args=[state],
        id="candle_refresh",
        name="Refresco de velas (1h/4h/12h, 14:30 UTC = 9:30 Colombia)",
        misfire_grace_time=REFRESCO_VELAS_GRACIA_SEGUNDOS,
    )

    # Job 4: Heartbeat — cada 5 min (y ping al dead-man's-switch si se configuró)
    scheduler.add_job(
        _job_heartbeat,
        trigger=IntervalTrigger(minutes=HEARTBEAT_INTERVAL_MINUTES),
        args=[settings.scheduler_deadman_url],
        id="heartbeat",
        name="Heartbeat (cada 5 min)",
    )

    # Listener de errores (red de seguridad)
    scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)

    # ── Señales para shutdown limpio ──
    def _shutdown(signum: int, _frame: Any) -> None:
        sig_name = signal.Signals(signum).name
        logger.info(
            "Scheduler: recibida señal %s — apagando",
            sig_name,
            extra={"component": _COMPONENT},
        )
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── Arrancar ──
    logger.info(
        "Scheduler: arrancando 5 jobs periódicos — monitor(60s), horario(:05), "
        "diario(00:05), refresco de velas(14:30 UTC), heartbeat(5min) — "
        "más la recuperación única de velas vencidas",
        extra={"component": _COMPONENT},
    )

    # TODO (task #68): agregar job de copia de datos a Railway cuando se
    # implemente la copia incremental (el script actual requiere destino
    # vacío y no sirve para ejecución periódica).

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info(
            "Scheduler: detenido",
            extra={"component": _COMPONENT},
        )
    finally:
        process_lock.release()


# ═══════════════════════════════════════════════════════════════════════
# __main__ (python -m app.orchestration.scheduler)
# ═══════════════════════════════════════════════════════════════════════


if __name__ == "__main__":
    start_scheduler()
