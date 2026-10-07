"""Servicios de lectura de la interfaz (Paso 10a) contra Postgres real.

Se reutiliza el escenario del Paso 9 (seis operaciones cerradas con score
alto ganando y score bajo perdiendo) para comprobar que cada vista muestra lo
que quedó guardado, sin recalcular ni escribir.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.data.exchange_client import Candle
from app.domain.enums import (
    EstadoCiclo,
    EstadoOrden,
    EstadoPosicion,
    EstadoTesis,
    IntencionOrden,
    ResultadoOperacion,
    TierEntrada,
)
from app.domain.rules.execution import CandidatoEntrada
from app.execution.order_manager import open_entries
from app.repositories.ai_calls import LlamadaIARow, save_llamada_ia
from app.repositories.analysis import create_ciclo, finalizar_ciclo, save_propuesta
from app.repositories.candles import save_candles
from app.services import views
from app.services.dashboard import get_dashboard
from app.services.decisions import ResultadoCiclo, get_ciclo_detalle, list_ciclos
from app.services.performance import get_performance_view
from app.services.positions import get_positions_view
from app.services.session import read_session
from app.services.system import get_system_view
from app.services.trades import FiltroOperaciones, get_operacion_detalle, list_operaciones
from app.services.validation import EstadoPregunta, get_validation_view
from tests.integration.test_analysis_repository import _propuesta_row
from tests.integration.test_results_pipeline import (  # noqa: F401
    CAPITAL,
    FakePrices,
    _abrir,
    _t,
    _tick,
    _universo,
    seis_operaciones,
)

D = Decimal
AHORA = _t(0, 30)


class _Parada:
    def __init__(self, activa: bool):
        self._activa = activa

    def is_active(self) -> bool:
        return self._activa


# --- solo lectura garantizada por la base ---


def test_read_session_rechaza_cualquier_escritura(db_engine):
    factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    with read_session(factory) as session:
        assert session.execute(text("SELECT 1")).scalar_one() == 1
        with pytest.raises(DBAPIError, match="read-only"):
            session.execute(text("CREATE TABLE zz_escritura_prohibida (a int)"))


def test_read_session_no_deja_nada_comprometido(db_engine):
    factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    with read_session(factory) as session:
        session.execute(text("SELECT count(*) FROM posiciones"))
    with read_session(factory) as session:  # la segunda sesión también arranca limpia y de solo lectura
        with pytest.raises(DBAPIError, match="read-only"):
            session.execute(text("DELETE FROM equity_observaciones"))


# --- dashboard ---


def test_dashboard_sin_datos_no_inventa_nada(db_session):
    v = get_dashboard(db_session, initial_capital=CAPITAL, now=AHORA, emergency_stop=_Parada(False))
    assert v.equity_actual is None and v.drawdown_actual_pct is None
    assert v.pico_equity == CAPITAL
    assert v.metricas.total_trades == 0 and v.metricas.win_rate_pct is None
    assert (v.curva_equity, v.curva_drawdown, v.curva_benchmark_btc) == ((), (), ())
    assert v.sistema_pausado is False and v.parada_emergencia is False and v.posiciones_abiertas == 0


def test_dashboard_con_operaciones_muestra_metricas_y_curvas(db_session, seis_operaciones):  # noqa: F811
    v = get_dashboard(db_session, initial_capital=CAPITAL, now=AHORA, emergency_stop=_Parada(True))
    assert v.parada_emergencia is True
    assert v.metricas.total_trades == 6 and v.metricas.win_rate_pct == D("50")
    assert v.equity_actual is not None and v.drawdown_actual_pct is not None
    assert len(v.curva_equity) > 1 and len(v.curva_equity) == len(v.curva_drawdown)
    assert min(p.value for p in v.curva_drawdown) <= 0
    assert [p.time for p in v.curva_equity] == sorted(p.time for p in v.curva_equity)
    assert v.curva_benchmark_btc == ()  # sin velas de BTC en la base: no se inventa


def test_dashboard_benchmark_btc_arranca_en_el_capital_inicial(db_session, seis_operaciones):  # noqa: F811
    inicio = _t(0) - timedelta(hours=1)
    velas = [
        Candle(symbol="BTCUSDT", timeframe="1h", open_time=inicio + timedelta(hours=i), open=D(1), high=D(1),
               low=D(1), close=D(str(50000 + 500 * i)), volume=D(1))
        for i in range(0, 24 * 6)
    ]
    save_candles(db_session, velas)
    v = get_dashboard(db_session, initial_capital=CAPITAL, now=AHORA)
    assert v.curva_benchmark_btc and v.curva_benchmark_btc[0].value == CAPITAL
    assert v.curva_benchmark_btc[-1].value > CAPITAL


def test_dashboard_adelgaza_las_curvas_largas(db_session, seis_operaciones):  # noqa: F811
    v = get_dashboard(db_session, initial_capital=CAPITAL, now=AHORA, max_puntos=3)
    assert len(v.curva_equity) <= 3 and len(v.curva_drawdown) <= 3


# --- posiciones ---


def test_posiciones_muestra_abierta_con_su_ultimo_snapshot(db_session):
    pos = _abrir(db_session)
    _tick(db_session, _universo(BTCUSDT="104"), minutos=1)
    v = get_positions_view(db_session, initial_capital=CAPITAL, now=_t(2), emergency_stop=_Parada(False))
    (p,) = v.posiciones
    assert p.position_id == pos.id and p.symbol == "BTCUSDT" and p.entry_price == D("100")
    assert p.ultimo_precio == D("104") and p.pnl_no_realizado == D("4") and p.retorno_no_realizado_pct == D("4")
    assert p.stop_vigente == D("92") and p.objetivo_vigente == D("112")
    assert (p.distancia_stop_pct is not None and p.mae_pct == D("0") and p.mfe_pct == D("4"))
    assert v.maximo_posiciones == 4 and v.cupos_libres == 3 and v.entradas_pendientes == ()


def test_posiciones_sin_nada_abierto(db_session):
    v = get_positions_view(db_session, initial_capital=CAPITAL, now=AHORA)
    assert v.posiciones == () and v.cupos_libres == 4 and v.ultima_observacion_en is None


def test_entrada_pendiente_de_aprobacion_se_muestra_y_reserva_cupo(db_session):
    ciclo = create_ciclo(db_session, started_at=_t(0), status=EstadoCiclo.PREFILTER)
    row = dataclasses.replace(_propuesta_row(ciclo, "ETHUSDT"), tier_entrada=TierEntrada.MAYOR_RIESGO,
                              puntaje_ponderado=D("52"))
    pid = save_propuesta(db_session, row)
    open_entries(db_session, [CandidatoEntrada(pid, "ETHUSDT", TierEntrada.MAYOR_RIESGO, D("52"))],
                 now=_t(0), price_source=FakePrices({}))
    v = get_positions_view(db_session, initial_capital=CAPITAL, now=_t(5))
    (e,) = v.entradas_pendientes
    assert e.symbol == "ETHUSDT" and e.requested_usd == D("100") and e.proposal_id == pid
    assert v.posiciones == () and v.cupos_libres == 3


# --- operaciones ---


def test_operaciones_lista_las_seis_la_mas_reciente_primero(db_session, seis_operaciones):  # noqa: F811
    filas = list_operaciones(db_session)
    assert len(filas) == 6
    assert [f.exit_time for f in filas] == sorted((f.exit_time for f in filas), reverse=True)
    assert filas[0].symbol == "ADAUSDT" and filas[0].resultado == ResultadoOperacion.PERDEDORA
    assert filas[-1].symbol == "BTCUSDT" and filas[-1].resultado == ResultadoOperacion.GANADORA


def test_operaciones_filtros(db_session, seis_operaciones):  # noqa: F811
    def n(**kw):
        return len(list_operaciones(db_session, FiltroOperaciones(**kw)))

    assert n(resultado=ResultadoOperacion.GANADORA) == 3 and n(resultado=ResultadoOperacion.PERDEDORA) == 3
    assert n(resultado=ResultadoOperacion.EQUILIBRIO) == 0
    assert n(symbol="BTCUSDT") == 1 and n(symbol="DOGEUSDT") == 0
    assert n(score_min=D("65")) == 3 and n(score_max=D("55")) == 2 and n(score_min=D("50"), score_max=D("70")) == 3
    assert n(exit_reason=IntencionOrden.STOP_LOSS) == 3 and n(exit_reason=IntencionOrden.TRAILING_STOP) == 3
    assert n(thesis_status=EstadoTesis.INVALIDATED) == 3 and n(thesis_status=EstadoTesis.CONFIRMED) == 3
    assert n(exit_desde=_t(0, 4)) == 2 and n(exit_hasta=_t(0, 0, ) + timedelta(hours=1)) == 1


def test_detalle_de_operacion_trae_decision_congelada_factores_y_trayectoria(db_session, seis_operaciones):  # noqa: F811
    fila = list_operaciones(db_session, FiltroOperaciones(symbol="BTCUSDT"))[0]
    d = get_operacion_detalle(db_session, fila.position_id)
    assert d is not None and d.fila.position_id == fila.position_id
    assert d.resultado.symbol == "BTCUSDT" and d.resultado.net_pnl == fila.net_pnl
    assert d.decision is not None and d.decision.tesis == "Tesis de prueba."
    assert d.decision.invalidacion == "Invalidación de prueba." and len(d.decision.factores) == 4
    assert d.decision.decision_snapshot["symbol"] == "BTCUSDT"
    estados = [p.system_state for p in d.trayectoria]
    assert estados[0] == "ENTRY" and estados[-1] == "EXIT" and "TRAILING_ACTIVATED" in estados
    assert [p.time for p in d.trayectoria] == sorted(p.time for p in d.trayectoria)


def test_detalle_de_operacion_inexistente_es_none(db_session):
    assert get_operacion_detalle(db_session, uuid.uuid4()) is None


def test_detalle_adelgaza_la_rutina_pero_conserva_los_eventos(db_session, seis_operaciones):  # noqa: F811
    fila = list_operaciones(db_session, FiltroOperaciones(symbol="BTCUSDT"))[0]
    d = get_operacion_detalle(db_session, fila.position_id, max_puntos=3)
    estados = [p.system_state for p in d.trayectoria]
    assert estados[0] == "ENTRY" and estados[-1] == "EXIT"  # los eventos nunca se descartan


# --- performance y validación ---


def test_performance_historial_completo_y_por_dias_sin_escribir(db_session, seis_operaciones):  # noqa: F811
    v = get_performance_view(db_session, initial_capital=CAPITAL, now=AHORA)
    assert v.global_.total_trades == 6 and len(v.por_simbolo) == 6
    assert [m.scope_key for m in v.por_tercil] == ["SUPERIOR", "MEDIO", "INFERIOR"]
    assert db_session.execute(text("SELECT count(*) FROM performance_metrics")).scalar_one() == 0  # no escribe

    reciente = get_performance_view(db_session, initial_capital=CAPITAL, now=AHORA, days=26)
    assert reciente.global_.total_trades == 2 and reciente.dias == 26
    with pytest.raises(ValueError):
        get_performance_view(db_session, initial_capital=CAPITAL, now=AHORA, days=0)


def test_performance_coincide_con_lo_que_guarda_el_comando(db_session, seis_operaciones):  # noqa: F811
    from app.results.performance_service import recalculate_performance

    v = get_performance_view(db_session, initial_capital=CAPITAL, now=AHORA)
    guardadas = recalculate_performance(
        db_session, period_start=v.periodo_inicio, period_end=AHORA, initial_capital=CAPITAL, now=AHORA
    )
    assert guardadas[0] == v.global_


def test_validacion_expone_informe_terciles_y_seis_preguntas(db_session, seis_operaciones):  # noqa: F811
    v = get_validation_view(db_session, initial_capital=CAPITAL, now=AHORA)
    assert v.informe.n_trades == 6 and v.informe.status.value == "PRELIMINAR"
    assert len(v.terciles) == 3 and v.global_.total_trades == 6
    assert [p.numero for p in v.preguntas] == [1, 2, 3, 4, 5, 6]
    assert {p.numero for p in v.preguntas if p.estado == EstadoPregunta.PENDIENTE} == {4}


def test_validacion_sin_operaciones(db_session):
    v = get_validation_view(db_session, initial_capital=CAPITAL, now=AHORA)
    assert v.informe.status.value == "MUESTRA_INSUFICIENTE" and v.terciles == ()


# --- decisiones ---


def test_ciclos_resultado_derivado_como_documenta_estado_ciclo(db_session):
    en_curso = create_ciclo(db_session, started_at=_t(0, 1), status=EstadoCiclo.PREFILTER)
    sin = create_ciclo(db_session, started_at=_t(0, 2), status=EstadoCiclo.PREFILTER)
    finalizar_ciclo(db_session, sin, status=EstadoCiclo.CONSOLIDATE, finished_at=_t(5, 2), cost_usd=D("0.4"),
                    error_message=None)
    fallido = create_ciclo(db_session, started_at=_t(0, 3), status=EstadoCiclo.PREFILTER)
    finalizar_ciclo(db_session, fallido, status=EstadoCiclo.AI_ANALYSIS, finished_at=_t(5, 3), cost_usd=D("0.1"),
                    error_message="boom")
    con = create_ciclo(db_session, started_at=_t(0, 4), status=EstadoCiclo.PREFILTER)
    save_propuesta(db_session, _propuesta_row(con, "BTCUSDT"))
    finalizar_ciclo(db_session, con, status=EstadoCiclo.CONSOLIDATE, finished_at=_t(5, 4), cost_usd=D("0.5"),
                    error_message=None)

    resumenes = list_ciclos(db_session, limit=10)
    assert [r.ciclo_id for r in resumenes] == [con, fallido, sin, en_curso]  # el más reciente primero
    assert [r.resultado for r in resumenes] == [
        ResultadoCiclo.CON_PROPUESTAS, ResultadoCiclo.FALLIDO, ResultadoCiclo.SIN_PROPUESTAS, ResultadoCiclo.EN_CURSO,
    ]
    assert resumenes[0].n_propuestas == 1 and resumenes[0].n_sobre_umbral == 1
    assert len(list_ciclos(db_session, limit=2)) == 2


def test_detalle_de_ciclo_reconstruye_candidatos_y_razon_de_las_llamadas(db_session):
    ciclo = create_ciclo(db_session, started_at=_t(0), status=EstadoCiclo.PREFILTER)
    for simbolo in ("BTCUSDT", "ETHUSDT", "BTCUSDT"):
        save_llamada_ia(db_session, LlamadaIARow(
            agente="analista_cuantitativo", modelo="m", prompt_version="v1", input_tokens=10, output_tokens=5,
            cost_usd=D("0.01"), respuesta={"simbolo": simbolo}, ciclo_id=ciclo))
    save_llamada_ia(db_session, LlamadaIARow(
        agente="analista_cuantitativo", modelo="m", prompt_version="v1", input_tokens=10, output_tokens=0,
        cost_usd=D("0.01"), respuesta=None, exitosa=False, error_message="json inválido", ciclo_id=ciclo))
    save_llamada_ia(db_session, LlamadaIARow(
        agente="consolidador", modelo="opus", prompt_version="v2", input_tokens=100, output_tokens=50,
        cost_usd=D("0.2"), ciclo_id=ciclo,
        respuesta={"recomendacion": "no_invertir", "razon_no_invertir": "Sin tesis.", "confianza_global": "media"}))

    d = get_ciclo_detalle(db_session, ciclo)
    # Las llamadas insertadas en una misma transacción comparten `created_at`:
    # el orden entre ellas no es significativo, solo el conjunto de candidatos.
    assert sorted(d.candidatos_evaluados) == ["BTCUSDT", "ETHUSDT"]
    assert (d.recomendacion, d.razon_no_invertir, d.confianza_global) == ("no_invertir", "Sin tesis.", "media")
    assert len(d.llamadas) == 5 and sum(1 for ll in d.llamadas if not ll.exitosa) == 1
    assert d.propuestas == () and "pre-filtro" in d.nota_embudo
    assert get_ciclo_detalle(db_session, uuid.uuid4()) is None


def test_detalle_de_ciclo_enlaza_propuesta_orden_y_posicion(db_session):
    pos = _abrir(db_session)
    propuesta_id = pos.proposal_id
    from app.repositories.analysis import get_propuesta

    ciclo_id = get_propuesta(db_session, propuesta_id).ciclo_id
    (p,) = get_ciclo_detalle(db_session, ciclo_id).propuestas
    assert p.proposal_id == propuesta_id and p.estado_entrada == EstadoOrden.FILLED.value
    assert p.position_id == pos.id and p.estado_posicion == EstadoPosicion.OPEN.value
    assert len(p.factores) == 4 and p.tier_entrada == "NORMAL"


# --- sistema ---


def test_sistema_sin_datos(db_session):
    v = get_system_view(db_session, initial_capital=CAPITAL, now=AHORA, emergency_stop=_Parada(False))
    assert v.base_de_datos_ok is True and v.discrepancias == ()
    assert v.ultima_observacion_monitor is None and v.edad_monitor is None
    assert [(x.timeframe, x.ultima_vela, x.edad) for x in v.velas] == [("1h", None, None), ("1d", None, None)]
    assert v.costo_ia.llamadas == 0 and v.costo_ia.costo_usd == 0 and v.saldo_ia is None and v.ciclos_recientes == ()


def test_sistema_con_actividad_monitor_velas_costo_y_saldo(db_session, seis_operaciones):  # noqa: F811
    save_candles(db_session, [Candle(symbol="BTCUSDT", timeframe="1d", open_time=AHORA - timedelta(hours=22),
                                     open=D(1), high=D(1), low=D(1), close=D(1), volume=D(1))])
    ciclo = create_ciclo(db_session, started_at=AHORA - timedelta(hours=1), status=EstadoCiclo.PREFILTER)
    save_llamada_ia(db_session, LlamadaIARow(
        agente="auditor_datos", modelo="h", prompt_version="v1", input_tokens=1, output_tokens=1,
        cost_usd=D("1.25"), respuesta={}, ciclo_id=ciclo))
    save_llamada_ia(db_session, LlamadaIARow(
        agente="auditor_datos", modelo="h", prompt_version="v1", input_tokens=1, output_tokens=0,
        cost_usd=D("0.25"), respuesta=None, exitosa=False, error_message="x", ciclo_id=ciclo))
    # `created_at` lo pone la base (now() real): se fija para que la prueba no dependa de la fecha de hoy.
    db_session.execute(text("UPDATE llamadas_ia SET created_at = :t WHERE ciclo_id = :c"),
                       {"t": AHORA - timedelta(days=1), "c": ciclo})

    v = get_system_view(
        db_session, initial_capital=CAPITAL, now=AHORA, emergency_stop=_Parada(True),
        saldo_declarado_usd=D("5.50"), saldo_declarado_en=AHORA - timedelta(days=2),
    )
    assert v.parada_emergencia is True and v.edad_monitor is not None and v.edad_monitor > timedelta(0)
    assert next(x for x in v.velas if x.timeframe == "1d").edad == timedelta(hours=22)
    assert (v.costo_ia.llamadas, v.costo_ia.fallidas, v.costo_ia.costo_usd) == (2, 1, D("1.50"))
    assert v.saldo_ia.gasto_desde_entonces_usd == D("1.50") and v.saldo_ia.saldo_estimado_usd == D("4.00")
    assert len(v.ciclos_recientes) >= 1


def test_sistema_reporta_discrepancias_de_la_reconciliacion(db_session):
    pos = _abrir(db_session)
    db_session.execute(text("UPDATE posiciones SET remaining_quantity = 0.5 WHERE id = :i"), {"i": pos.id})
    db_session.expire_all()  # el UPDATE crudo no pasa por el mapa de identidad de la sesión
    v = get_system_view(db_session, initial_capital=CAPITAL, now=_t(5))
    assert len(v.discrepancias) >= 1


# --- fachada ---


def test_la_fachada_abre_sesion_de_solo_lectura_y_devuelve_vistas(db_engine, monkeypatch):
    factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("app.services.session._factory", lambda: factory)
    assert views.operaciones() == ()
    assert views.ciclos() == () or isinstance(views.ciclos(), tuple)
    assert views.dashboard().metricas.total_trades >= 0
    assert views.posiciones().maximo_posiciones == 4
    assert views.performance(days=7).dias == 7
    assert views.validacion().preguntas
    assert views.sistema().base_de_datos_ok is True
    assert views.operacion_detalle(uuid.uuid4()) is None and views.ciclo_detalle(uuid.uuid4()) is None
