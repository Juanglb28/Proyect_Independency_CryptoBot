"""Recuperación tras morir y revivir (Paso 11c; principio P5) contra Postgres real.

Cada "proceso" usa SESIONES NUEVAS de una misma fábrica: lo que un proceso no
confirmó (`commit`) se pierde al morir, y el siguiente solo ve lo confirmado.
La fábrica comparte una conexión con una transacción exterior que se revierte
al final del test, así los `commit()` del código bajo prueba no dejan datos en
la base (ver `reinicios`). El candado de proceso único sí usa conexiones
propias y un `SIGKILL` real (último test).
"""

from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.db.session import session_scope
from app.domain.enums import (
    EstadoCiclo,
    EstadoOrden,
    EstadoPosicion,
    IntencionOrden,
    TierEntrada,
    TipoAlerta,
)
from app.domain.rules.execution import CandidatoEntrada
from app.domain.rules.recovery import MENSAJE_CICLO_INTERRUMPIDO
from app.execution.monitor import ReconciliationBlocked, run_monitor_tick
from app.execution.order_manager import (
    ResultadoAprobacion,
    ResultadoEntrada,
    approve_pending_entry,
    open_entries,
)
from app.execution.price_source import PriceUnavailable
from app.orchestration.lifecycle import run_startup_recovery
from app.repositories.ai_calls import LlamadaIARow, save_llamada_ia
from app.repositories.alertas import list_alertas
from app.repositories.analysis import (
    create_ciclo,
    finalizar_ciclo,
    get_ciclo,
    list_ciclos_recientes,
    save_propuesta,
)
from app.repositories.orders import list_orders
from app.repositories.positions import get_posicion, list_open_posiciones, list_posiciones
from app.repositories.process_lock import SchedulerLock
from app.repositories.trade_results import list_trade_results
from tests.integration.test_analysis_repository import _propuesta_row

D = Decimal
CAPITAL = D("1000")
_AHORA = datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc)  # 03:00 UTC: ya pasó el ciclo de las 00:05


class FakePrices:
    def __init__(self, precios: dict[str, str] | None = None):
        self.precios = {k: D(v) for k, v in (precios or {}).items()}

    def get_price(self, symbol: str) -> Decimal:
        if symbol not in self.precios:
            raise PriceUnavailable(symbol)
        return self.precios[symbol]


def _propuesta(fabrica, symbol="BTCUSDT", tier=TierEntrada.NORMAL, puntaje="64.60", now=_AHORA) -> CandidatoEntrada:
    with session_scope(fabrica) as s:
        ciclo = create_ciclo(s, started_at=now, status=EstadoCiclo.PREFILTER)
        finalizar_ciclo(
            s, ciclo, status=EstadoCiclo.FREEZE_DECISION_SNAPSHOT, finished_at=now, cost_usd=D("0"), error_message=None
        )
        row = dataclasses.replace(_propuesta_row(ciclo, symbol), tier_entrada=tier, puntaje_ponderado=D(puntaje))
        pid = save_propuesta(s, row)
    return CandidatoEntrada(pid, symbol, tier, D(puntaje))


def _abrir(fabrica, candidato, precio="100", now=_AHORA):
    with session_scope(fabrica) as s:
        return open_entries(s, [candidato], now=now, price_source=FakePrices({candidato.symbol: precio}))


def _tick(session, precio, minutos=1):
    return run_monitor_tick(
        session, now=_AHORA + timedelta(minutes=minutos), price_source=FakePrices({"BTCUSDT": precio}),
        initial_capital=CAPITAL,
    )


def _ventas(fabrica):
    with session_scope(fabrica) as s:
        return [o for o in list_orders(s, estados=[EstadoOrden.FILLED]) if o.side.value == "SELL"]


def _corromper(fabrica, position_id):
    """La cantidad restante deja de explicarse con las órdenes."""
    with session_scope(fabrica) as s:
        s.execute(text("UPDATE posiciones SET remaining_quantity = 0.5 WHERE id = :id"), {"id": position_id})


# --- ciclos huérfanos ---


def _ciclo_huerfano(fabrica, *, etapa=EstadoCiclo.AI_ANALYSIS, inicio=None, costos=("0.10", "0.25")):
    with session_scope(fabrica) as s:
        ciclo_id = create_ciclo(s, started_at=inicio or _AHORA - timedelta(hours=2), status=etapa)
        for costo in costos:
            save_llamada_ia(
                s,
                LlamadaIARow(
                    agente="analista_cuantitativo", modelo="m", prompt_version="v1", input_tokens=1,
                    output_tokens=1, cost_usd=D(costo), respuesta={"ok": True}, ciclo_id=ciclo_id,
                ),
            )
    return ciclo_id


def test_un_ciclo_a_medias_se_marca_interrumpido_conservando_etapa_y_costo_real(reinicios):
    ciclo_id = _ciclo_huerfano(reinicios, etapa=EstadoCiclo.AI_ANALYSIS)

    reporte = run_startup_recovery(reinicios, now=_AHORA)

    assert reporte.ciclos_interrumpidos == (ciclo_id,)
    with session_scope(reinicios) as s:
        ciclo = get_ciclo(s, ciclo_id)
        assert ciclo.finished_at == _AHORA
        assert ciclo.status == EstadoCiclo.AI_ANALYSIS  # la última etapa alcanzada se conserva
        assert ciclo.error_message == MENSAJE_CICLO_INTERRUMPIDO
        assert ciclo.cost_usd == D("0.35")  # lo que Anthropic ya cobró queda registrado


def test_marcar_huerfanos_es_idempotente_entre_reinicios(reinicios):
    _ciclo_huerfano(reinicios)
    run_startup_recovery(reinicios, now=_AHORA)
    segundo = run_startup_recovery(reinicios, now=_AHORA + timedelta(minutes=5))
    assert segundo.ciclos_interrumpidos == ()


def test_un_ciclo_terminado_nunca_se_toca(reinicios):
    _propuesta(reinicios, now=_AHORA - timedelta(hours=1))  # ciclo ya finalizado, con su propuesta
    with session_scope(reinicios) as s:
        antes = [(c.finished_at, c.error_message, c.status) for c in list_ciclos_recientes(s, limit=10)]

    reporte = run_startup_recovery(reinicios, now=_AHORA)

    assert reporte.ciclos_interrumpidos == ()
    with session_scope(reinicios) as s:
        assert [(c.finished_at, c.error_message, c.status) for c in list_ciclos_recientes(s, limit=10)] == antes


# --- ciclo diario perdido ---


def _alertas_de(fabrica, tipo):
    with session_scope(fabrica) as s:
        return [a for a in list_alertas(s, limit=50) if a.tipo == tipo]


def test_sin_ciclo_completo_de_hoy_se_avisa_una_sola_vez_aunque_se_reinicie_varias_veces(reinicios):
    r1 = run_startup_recovery(reinicios, now=_AHORA)
    r2 = run_startup_recovery(reinicios, now=_AHORA + timedelta(minutes=10))
    r3 = run_startup_recovery(reinicios, now=_AHORA + timedelta(hours=5))

    assert r1.ciclo_diario_perdido and r2.ciclo_diario_perdido and r3.ciclo_diario_perdido
    assert r1.avisos_creados == (TipoAlerta.CICLO_DIARIO_PERDIDO,)
    assert r2.avisos_creados == () and r3.avisos_creados == ()
    avisos = _alertas_de(reinicios, TipoAlerta.CICLO_DIARIO_PERDIDO)
    assert len(avisos) == 1 and "2026-10-10" in avisos[0].message and "NO se relanzó" in avisos[0].message


def test_el_aviso_menciona_los_ciclos_que_quedaron_a_medias(reinicios):
    _ciclo_huerfano(reinicios, inicio=datetime(2026, 10, 10, 0, 5, tzinfo=timezone.utc))
    reporte = run_startup_recovery(reinicios, now=_AHORA)
    assert reporte.ciclo_diario_perdido
    assert "1 ciclo(s) a medias" in _alertas_de(reinicios, TipoAlerta.CICLO_DIARIO_PERDIDO)[0].message


def test_con_un_ciclo_completo_de_hoy_no_se_avisa(reinicios):
    _propuesta(reinicios, now=datetime(2026, 10, 10, 0, 5, tzinfo=timezone.utc))
    reporte = run_startup_recovery(reinicios, now=_AHORA)
    assert reporte.ciclo_diario_perdido is False and reporte.avisos_creados == ()
    assert _alertas_de(reinicios, TipoAlerta.CICLO_DIARIO_PERDIDO) == []


def test_antes_de_las_00_05_no_falta_ningun_ciclo(reinicios):
    reporte = run_startup_recovery(reinicios, now=datetime(2026, 10, 10, 0, 4, tzinfo=timezone.utc))
    assert reporte.ciclo_diario_perdido is False


def test_recuperar_nunca_relanza_el_ciclo(reinicios):
    run_startup_recovery(reinicios, now=_AHORA)
    with session_scope(reinicios) as s:
        n = s.execute(text("SELECT count(*) FROM ciclos_analisis")).scalar_one()
        assert n == 0  # solo avisó; no creó ningún ciclo ni llamó a la IA


# --- reconciliación al arrancar y bloqueo de entradas ---


def _estado_con_posicion(fabrica):
    candidato = _propuesta(fabrica)
    _abrir(fabrica, candidato)
    with session_scope(fabrica) as s:
        return list_open_posiciones(s)[0].id


def test_estado_sano_al_arrancar(reinicios):
    _estado_con_posicion(reinicios)
    reporte = run_startup_recovery(reinicios, now=_AHORA)
    assert reporte.discrepancias == 0 and reporte.errores == ()
    assert _alertas_de(reinicios, TipoAlerta.DISCREPANCIA_RECONCILIACION) == []


def test_una_discrepancia_al_arrancar_avisa_y_bloquea_monitor_y_entradas(reinicios):
    position_id = _estado_con_posicion(reinicios)
    _corromper(reinicios, position_id)

    reporte = run_startup_recovery(reinicios, now=_AHORA)

    assert reporte.discrepancias == 1 and reporte.estado_sano is False
    assert TipoAlerta.DISCREPANCIA_RECONCILIACION in reporte.avisos_creados
    assert len(_alertas_de(reinicios, TipoAlerta.DISCREPANCIA_RECONCILIACION)) == 1

    # El monitor no actúa...
    with session_scope(reinicios) as s, pytest.raises(ReconciliationBlocked):
        _tick(s, "50")
    # ...y tampoco se abre ninguna entrada nueva.
    otro = _propuesta(reinicios, symbol="ETHUSDT")
    resultado = _abrir(reinicios, otro, precio="2000")
    assert resultado.bloqueada_por_reconciliacion and resultado.procesadas == ()
    with session_scope(reinicios) as s:
        assert len(list_posiciones(s)) == 1
        assert len(list_orders(s, estados=[EstadoOrden.FILLED])) == 1


def test_aprobar_se_bloquea_por_discrepancia_y_la_orden_sigue_pendiente_pero_rechazar_funciona(reinicios):
    from app.execution.order_manager import reject_pending_entry

    position_id = _estado_con_posicion(reinicios)
    pendiente = _propuesta(reinicios, symbol="ETHUSDT", tier=TierEntrada.MAYOR_RIESGO, puntaje="58.00")
    resultado = _abrir(reinicios, pendiente, precio="2000")
    assert resultado.procesadas[0].resultado == ResultadoEntrada.PENDIENTE_APROBACION
    orden_id = resultado.procesadas[0].orden_id
    _corromper(reinicios, position_id)

    with session_scope(reinicios) as s:
        aprobacion = approve_pending_entry(s, orden_id, now=_AHORA, price_source=FakePrices({"ETHUSDT": "2000"}))
    assert aprobacion == ResultadoAprobacion.BLOQUEADA_POR_RECONCILIACION
    with session_scope(reinicios) as s:
        assert len(list_posiciones(s)) == 1
        assert [o.estado for o in list_orders(s, estados=[EstadoOrden.PENDING_APPROVAL])] == [EstadoOrden.PENDING_APPROVAL]
        assert reject_pending_entry(s, orden_id, now=_AHORA) is True  # rechazar sigue permitido


# --- morir y revivir con posición abierta ---


def test_muerte_antes_de_confirmar_un_stop_no_deja_nada_a_medias_y_el_reinicio_lo_ejecuta_una_sola_vez(reinicios):
    candidato = _propuesta(reinicios)
    _abrir(reinicios, candidato, precio="100")

    # Proceso 1: ejecuta el stop pero muere ANTES del commit.
    sesion = reinicios()
    resultado = _tick(sesion, "90", 1)
    assert [c.intencion for c in resultado.chequeos] == [IntencionOrden.STOP_LOSS]
    sesion.rollback()
    sesion.close()

    # Lo no confirmado desapareció: la posición sigue abierta y entera, sin ventas ni resultados.
    with session_scope(reinicios) as s:
        pos = list_open_posiciones(s)
        assert len(pos) == 1 and pos[0].remaining_quantity == pos[0].initial_quantity
        assert list_trade_results(s) == []
    assert _ventas(reinicios) == []

    # Proceso 2: arranca, recupera y vigila con el precio ACTUAL.
    reporte = run_startup_recovery(reinicios, now=_AHORA + timedelta(minutes=2))
    assert reporte.discrepancias == 0
    with session_scope(reinicios) as s:
        res = _tick(s, "88", 3)
    assert [c.intencion for c in res.chequeos] == [IntencionOrden.STOP_LOSS]

    # Proceso 3: otro reinicio y otro chequeo: no hay nada que repetir.
    with session_scope(reinicios) as s:
        otra = _tick(s, "88", 4)
    assert otra.chequeos == ()

    ventas = _ventas(reinicios)
    assert len(ventas) == 1 and ventas[0].execution_price == D("88")  # precio del reinicio, no el del proceso muerto
    with session_scope(reinicios) as s:
        assert [p.status for p in list_posiciones(s)] == [EstadoPosicion.CLOSED]
        assert len(list_trade_results(s)) == 1  # un único resultado definitivo
    assert run_startup_recovery(reinicios, now=_AHORA + timedelta(minutes=10)).discrepancias == 0


def test_tras_un_tp_parcial_confirmado_el_reinicio_no_vende_otra_vez_y_el_trailing_sigue(reinicios):
    candidato = _propuesta(reinicios)
    _abrir(reinicios, candidato, precio="100")
    with session_scope(reinicios) as s:
        res = _tick(s, "113", 1)
    assert [c.intencion for c in res.chequeos] == [IntencionOrden.TAKE_PROFIT_PARTIAL]

    run_startup_recovery(reinicios, now=_AHORA + timedelta(minutes=2))  # revive
    with session_scope(reinicios) as s:
        res = _tick(s, "113", 3)
        assert [c.accion.value for c in res.chequeos] == ["MANTENER"]
    with session_scope(reinicios) as s:
        pos = list_open_posiciones(s)[0]
        assert pos.take_profit_hit and pos.trailing_active and pos.remaining_quantity == D("0.5")
    assert len(_ventas(reinicios)) == 1


def test_reintentar_la_misma_entrada_tras_un_reinicio_no_duplica_orden_ni_posicion(reinicios):
    candidato = _propuesta(reinicios)
    primero = _abrir(reinicios, candidato)
    assert primero.procesadas[0].resultado == ResultadoEntrada.ABIERTA

    run_startup_recovery(reinicios, now=_AHORA + timedelta(minutes=1))
    segundo = _abrir(reinicios, candidato, now=_AHORA + timedelta(minutes=2))

    assert all(p.resultado == ResultadoEntrada.YA_PROCESADA for p in segundo.procesadas) or segundo.procesadas == ()
    with session_scope(reinicios) as s:
        assert len(list_posiciones(s)) == 1
        assert len(list_orders(s, estados=[EstadoOrden.FILLED])) == 1


def test_muerte_a_mitad_de_una_entrada_no_deja_posicion_fantasma(reinicios):
    candidato = _propuesta(reinicios)
    sesion = reinicios()
    resultado = open_entries(sesion, [candidato], now=_AHORA, price_source=FakePrices({"BTCUSDT": "100"}))
    assert resultado.procesadas[0].resultado == ResultadoEntrada.ABIERTA
    sesion.rollback()  # muere antes del commit
    sesion.close()

    with session_scope(reinicios) as s:
        assert list_posiciones(s) == [] and list_orders(s, estados=[EstadoOrden.FILLED]) == []
    assert run_startup_recovery(reinicios, now=_AHORA + timedelta(minutes=1)).discrepancias == 0

    # El reintento del nuevo proceso sí abre, una sola vez.
    reintento = _abrir(reinicios, candidato, now=_AHORA + timedelta(minutes=2))
    assert reintento.procesadas[0].resultado == ResultadoEntrada.ABIERTA
    with session_scope(reinicios) as s:
        assert len(list_posiciones(s)) == 1 and get_posicion(s, reintento.procesadas[0].posicion_id) is not None


def test_una_falla_de_un_paso_de_la_recuperacion_no_impide_los_demas(reinicios, monkeypatch):
    from app.orchestration import lifecycle

    def _boom(*_a, **_k):
        raise RuntimeError("falla simulada")

    monkeypatch.setattr(lifecycle, "interrumpir_ciclos_huerfanos", _boom)
    reporte = run_startup_recovery(reinicios, now=_AHORA)
    assert len(reporte.errores) == 1 and "ciclos huérfanos" in reporte.errores[0]
    assert reporte.ciclo_diario_perdido is True  # los otros pasos corrieron
    assert reporte.estado_sano is False


# --- candado de proceso único ---


def test_solo_un_scheduler_puede_tomar_el_candado_y_se_libera_al_soltarlo(db_engine):
    a, b = SchedulerLock(db_engine), SchedulerLock(db_engine)
    try:
        assert a.try_acquire() is True and a.held
        assert a.try_acquire() is True  # idempotente
        assert b.try_acquire() is False and not b.held
        a.release()
        assert b.try_acquire() is True
    finally:
        a.release()
        b.release()


def test_el_candado_se_libera_solo_cuando_el_proceso_muere_con_sigkill(db_engine):
    url = db_engine.url.render_as_string(hide_password=False)
    codigo = textwrap.dedent(
        """
        import sys, time
        from sqlalchemy import create_engine
        from app.repositories.process_lock import SchedulerLock
        lock = SchedulerLock(create_engine(sys.argv[1]))
        print("OBTENIDO" if lock.try_acquire() else "OCUPADO", flush=True)
        time.sleep(60)
        """
    )
    proceso = subprocess.Popen(
        [sys.executable, "-c", codigo, url], stdout=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    propio = SchedulerLock(db_engine)
    try:
        assert proceso.stdout.readline().strip() == "OBTENIDO"
        assert propio.try_acquire() is False  # el proceso vivo lo tiene

        proceso.send_signal(signal.SIGKILL)  # muerte sin limpieza alguna
        proceso.wait(timeout=10)

        for _ in range(50):  # Postgres detecta la conexión caída y libera el candado
            if propio.try_acquire():
                break
            time.sleep(0.1)
        assert propio.held
    finally:
        propio.release()
        if proceso.poll() is None:
            proceso.kill()
        proceso.stdout.close()
