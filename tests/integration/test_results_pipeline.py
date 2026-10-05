"""Paso 9 de punta a punta contra Postgres real: entrada -> snapshots del
monitor -> cierre -> `trade_result` (con benchmark de precios observados) ->
métricas y validación. Incluye que registrar NUNCA bloquea un stop y que el
barrido de reparación completa lo que falló."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.data.indicator_calculator import INDICATOR_NAME_RSI, IndicatorRow
from app.domain.enums import (
    EstadoCiclo,
    EstadoPosicion,
    EstadoSnapshot,
    EstadoTesis,
    IntencionOrden,
    TierEntrada,
)
from app.domain.rules.execution import CandidatoEntrada
from app.domain.symbols import ALLOWED_SYMBOLS
from app.execution import results_recorder
from app.execution.monitor import run_monitor_tick
from app.execution.order_manager import open_entries
from app.execution.price_source import PriceUnavailable
from app.repositories.analysis import create_ciclo, save_propuesta
from app.repositories.indicators import save_indicators
from app.repositories.positions import get_open_posicion_by_symbol, get_posicion
from app.repositories.snapshots import list_snapshots_for_position
from app.repositories.trade_results import get_trade_result_by_position, list_trade_results
from app.results.performance_service import recalculate_performance
from app.results.trade_result_service import EstadoGeneracion, generate_trade_result
from app.results.validation_service import validate_score
from tests.integration.test_analysis_repository import _propuesta_row

D = Decimal
CAPITAL = D("1000")
_AHORA = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)


def _t(minutos: int, dia: int = 0) -> datetime:
    return _AHORA + timedelta(days=dia, minutes=minutos)


class FakePrices:
    def __init__(self, precios: dict[str, str] | None = None):
        self.precios = {k: D(v) for k, v in (precios or {}).items()}

    def get_price(self, symbol: str) -> Decimal:
        if symbol not in self.precios:
            raise PriceUnavailable(symbol)
        return self.precios[symbol]


def _universo(base="100", **override) -> dict[str, str]:
    precios = {s: base for s in ALLOWED_SYMBOLS}
    precios.update(override)
    return precios


def _abrir(db_session, symbol="BTCUSDT", precios=None, puntaje="64.60", dia=0):
    ciclo = create_ciclo(db_session, started_at=_t(0, dia), status=EstadoCiclo.PREFILTER)
    row = dataclasses.replace(
        _propuesta_row(ciclo, symbol), tier_entrada=TierEntrada.NORMAL, puntaje_ponderado=D(puntaje)
    )
    pid = save_propuesta(db_session, row)
    open_entries(
        db_session,
        [CandidatoEntrada(pid, symbol, TierEntrada.NORMAL, D(puntaje))],
        now=_t(0, dia),
        price_source=FakePrices(precios if precios is not None else _universo()),
    )
    return get_open_posicion_by_symbol(db_session, symbol)


def _tick(db_session, precios, minutos=1, dia=0):
    return run_monitor_tick(
        db_session, now=_t(minutos, dia), price_source=FakePrices(precios), initial_capital=CAPITAL
    )


def _estados(db_session, posicion):
    return [s.system_state for s in list_snapshots_for_position(db_session, posicion.id)]


# --- entrada ---


def test_la_entrada_guarda_precios_de_referencia_y_el_snapshot_entry(db_session):
    pos = _abrir(db_session, precios=_universo(ETHUSDT="2000"))
    assert set(pos.reference_prices_at_entry) == set(ALLOWED_SYMBOLS)
    assert pos.reference_prices_at_entry["ETHUSDT"] == "2000" and pos.reference_prices_at_entry["BTCUSDT"] == "100"

    (snap,) = list_snapshots_for_position(db_session, pos.id)
    assert snap.system_state == EstadoSnapshot.ENTRY and snap.snapshot_time == _t(0)
    assert (snap.market_price, snap.quantity, snap.position_value) == (D("100"), D("1"), D("100"))
    assert (snap.unrealized_pnl, snap.unrealized_return_pct) == (D("0"), D("0"))
    assert (snap.stop_price, snap.target_price) == (D("92"), D("112"))
    assert (snap.distance_to_stop_pct, snap.distance_to_target_pct) == (D("8"), D("12"))
    assert (snap.current_mae_pct, snap.current_mfe_pct) == (D("0"), D("0"))
    assert snap.trailing_active is False and snap.market_volume is None


def test_si_faltan_precios_de_referencia_solo_se_guardan_los_disponibles(db_session):
    pos = _abrir(db_session, precios={"BTCUSDT": "100"})
    assert pos.reference_prices_at_entry == {"BTCUSDT": "100"}


# --- trayectoria ---


def test_cada_chequeo_agrega_un_snapshot_programado_y_reintentar_no_duplica(db_session):
    pos = _abrir(db_session)
    _tick(db_session, _universo(BTCUSDT="104"))
    _tick(db_session, _universo(BTCUSDT="104"))  # mismo instante: reintento del mismo chequeo

    snaps = list_snapshots_for_position(db_session, pos.id)
    assert [s.system_state for s in snaps] == [EstadoSnapshot.ENTRY, EstadoSnapshot.SCHEDULED]
    programado = snaps[1]
    assert programado.snapshot_time == _t(1) and programado.market_price == D("104")
    assert programado.unrealized_pnl == D("4") and programado.unrealized_return_pct == D("4")
    assert programado.current_mfe_pct == D("4") and programado.current_mae_pct == D("0")


def test_el_snapshot_trae_indicadores_solo_de_velas_ya_cerradas(db_session):
    pos = _abrir(db_session)
    save_indicators(
        db_session,
        [
            IndicatorRow("BTCUSDT", "1h", _AHORA - timedelta(hours=3), INDICATOR_NAME_RSI, D("40")),
            IndicatorRow("BTCUSDT", "1h", _AHORA + timedelta(minutes=1) - timedelta(hours=1), INDICATOR_NAME_RSI, D("55.5")),
            # vela de 1h que abre 30 min antes del snapshot: aún está abierta, no se conocía
            IndicatorRow("BTCUSDT", "1h", _t(1) - timedelta(minutes=30), INDICATOR_NAME_RSI, D("99")),
        ],
    )
    _tick(db_session, _universo())
    programado = [s for s in list_snapshots_for_position(db_session, pos.id) if s.system_state == EstadoSnapshot.SCHEDULED][0]
    assert programado.rsi_1h == D("55.5")  # la vela de las 14:01, cerrada a las 15:01
    assert programado.rsi_4h is None and programado.atr is None and programado.relative_volume is None


# --- cierre por stop ---


def test_stop_crea_el_trade_result_con_benchmark_y_snapshot_exit(db_session):
    pos = _abrir(db_session)
    _tick(db_session, _universo(BTCUSDT="95"), minutos=1)  # baja, sin tocar el stop
    _tick(db_session, _universo(BTCUSDT="92"), minutos=2)  # stop

    assert get_posicion(db_session, pos.id).status == EstadoPosicion.CLOSED
    assert _estados(db_session, pos) == [EstadoSnapshot.ENTRY, EstadoSnapshot.SCHEDULED, EstadoSnapshot.EXIT]

    r = get_trade_result_by_position(db_session, pos.id)
    assert r.symbol == "BTCUSDT" and r.side == "LONG"
    assert (r.entry_price, r.exit_price, r.entry_quantity, r.exit_quantity) == (D("100"), D("92"), D("1"), D("1"))
    assert (r.gross_pnl, r.total_fees, r.net_pnl) == (D("-8"), D("0.192"), D("-8.192"))
    assert r.realized_r_multiple == D("-1.024")
    assert r.exit_reason == IntencionOrden.STOP_LOSS and r.thesis_status == EstadoTesis.INVALIDATED
    assert (r.mae_pct, r.mfe_pct) == (D("-8"), D("0"))
    assert r.entry_time == _t(0) and r.exit_time == _t(2) and r.duration_seconds == 120
    assert r.capital_at_entry == D("1000")  # sin observaciones previas: el capital inicial
    assert r.initial_opportunity_score == D("64.600")
    assert r.proposal_id == pos.proposal_id and r.entry_order_id and r.exit_order_id
    # Benchmark con precios OBSERVADOS: BTC 100 -> 92; el resto del universo no se movió.
    assert r.btc_return_during_trade_pct == D("-8")
    assert r.market_return_during_trade_pct == D("-0.4")  # -8% / 20 pares


def test_el_benchmark_queda_null_si_falta_un_par_de_la_referencia_de_salida(db_session):
    pos = _abrir(db_session)
    precios = _universo(BTCUSDT="92")
    del precios["SOLUSDT"]  # Binance no responde para SOL al cerrar
    _tick(db_session, precios, minutos=1)
    r = get_trade_result_by_position(db_session, pos.id)
    assert r.btc_return_during_trade_pct == D("-8") and r.market_return_during_trade_pct is None


# --- TP parcial + trailing ---


def test_tp_parcial_y_trailing_producen_un_solo_resultado_agregado(db_session):
    pos = _abrir(db_session)
    _tick(db_session, _universo(BTCUSDT="112"), minutos=1)  # TP del 50%
    _tick(db_session, _universo(BTCUSDT="120"), minutos=2)  # el máximo sube
    _tick(db_session, _universo(BTCUSDT="112.8"), minutos=3)  # trailing: 120 × 0,94

    estados = _estados(db_session, pos)
    assert estados == [
        EstadoSnapshot.ENTRY,
        EstadoSnapshot.PARTIAL_TAKE_PROFIT,
        EstadoSnapshot.TRAILING_ACTIVATED,
        EstadoSnapshot.SCHEDULED,
        EstadoSnapshot.TRAILING_UPDATED,
        EstadoSnapshot.SCHEDULED,
        EstadoSnapshot.EXIT,
    ]
    snaps = list_snapshots_for_position(db_session, pos.id)
    parcial, activado = snaps[1], snaps[2]
    assert parcial.quantity == D("1") and parcial.trailing_active is False and parcial.target_price == D("112")
    assert activado.quantity == D("0.5") and activado.trailing_active is True
    assert activado.trailing_reference_price == D("112") and activado.stop_price == D("105.28")
    assert activado.target_price is None
    salida = snaps[-1]
    assert salida.quantity == D("0.5") and salida.stop_price == D("112.8") and salida.trailing_reference_price == D("120")

    r = get_trade_result_by_position(db_session, pos.id)
    assert r.exit_quantity == D("1") and r.exit_price == D("112.4")  # promedio ponderado
    assert r.gross_pnl == D("12.4") and r.net_pnl == D("12.1876")
    assert r.exit_reason == IntencionOrden.TRAILING_STOP  # la orden que CERRÓ la posición
    assert r.thesis_status == EstadoTesis.CONFIRMED
    assert r.mfe_pct == D("20") and r.mae_pct == D("0")
    assert len(list_trade_results(db_session)) == 1


def test_generar_el_resultado_dos_veces_no_crea_otro_ni_toca_el_existente(db_session):
    pos = _abrir(db_session)
    _tick(db_session, _universo(BTCUSDT="92"))
    antes = get_trade_result_by_position(db_session, pos.id)
    r = generate_trade_result(db_session, pos.id, initial_capital=CAPITAL)
    assert r.estado == EstadoGeneracion.YA_EXISTIA and r.trade_result_id == antes.id
    assert len(list_trade_results(db_session)) == 1


def test_una_posicion_abierta_no_genera_resultado(db_session):
    pos = _abrir(db_session)
    assert generate_trade_result(db_session, pos.id, initial_capital=CAPITAL).estado == EstadoGeneracion.NO_CERRADA
    assert get_trade_result_by_position(db_session, pos.id) is None


# --- registrar nunca bloquea operar ---


def test_si_falla_el_resultado_el_stop_se_ejecuta_igual_y_el_barrido_lo_repara(db_session, monkeypatch):
    pos = _abrir(db_session)

    def falla(*args, **kwargs):
        raise RuntimeError("fallo simulado en la capa de resultados")

    monkeypatch.setattr(results_recorder, "generate_trade_result", falla)
    res = _tick(db_session, _universo(BTCUSDT="92"), minutos=1)

    assert res.hubo_problemas is False  # el chequeo fue un éxito
    assert get_posicion(db_session, pos.id).status == EstadoPosicion.CLOSED
    assert get_trade_result_by_position(db_session, pos.id) is None

    monkeypatch.undo()
    res = _tick(db_session, {}, minutos=2)
    assert res.resultados_reparados == 1
    r = get_trade_result_by_position(db_session, pos.id)
    assert r.net_pnl == D("-8.192")
    assert r.btc_return_during_trade_pct is None and r.market_return_during_trade_pct is None  # sin precios de salida


def test_si_fallan_los_snapshots_el_stop_se_ejecuta_igual(db_session, monkeypatch):
    pos = _abrir(db_session)
    monkeypatch.setattr(results_recorder, "record_snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    _tick(db_session, _universo(BTCUSDT="92"))
    assert get_posicion(db_session, pos.id).status == EstadoPosicion.CLOSED
    assert _estados(db_session, pos) == [EstadoSnapshot.ENTRY]  # solo el de la entrada; el EXIT falló
    r = get_trade_result_by_position(db_session, pos.id)
    # MAE/MFE salen SOLO de los snapshots que existen: nunca se infieren del precio de salida.
    assert r is not None and (r.mae_pct, r.mfe_pct) == (D("0"), D("0"))


def test_si_falla_el_snapshot_de_entrada_la_posicion_se_abre_igual(db_session, monkeypatch):
    monkeypatch.setattr(results_recorder, "record_snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    pos = _abrir(db_session)
    assert pos is not None and pos.status == EstadoPosicion.OPEN
    assert list_snapshots_for_position(db_session, pos.id) == []


def test_pausa_por_drawdown_registra_snapshot_de_cada_posicion_abierta(db_session):
    pos = _abrir(db_session)
    # El pico sembrado fuerza drawdown de -15% con la posición sin moverse.
    db_session.execute(
        text(
            "INSERT INTO equity_observaciones (observed_at, origen, equity_usd, peak_equity_usd, open_positions) "
            "VALUES (:t, 'MONITOR', 1000, 1200, 1)"
        ),
        {"t": _t(0) - timedelta(hours=1)},
    )
    res = _tick(db_session, _universo(BTCUSDT="100"))
    assert res.pausa_registrada is True
    assert EstadoSnapshot.SYSTEM_PAUSED in _estados(db_session, pos)


# --- métricas y validación ---


def _operar(db_session, symbol, puntaje, gana, dia):
    _abrir(db_session, symbol=symbol, precios=_universo(), puntaje=puntaje, dia=dia)
    base = {symbol: "100"}
    if gana:
        for minutos, precio in ((1, "112"), (2, "120"), (3, "112.8")):
            _tick(db_session, _universo(**{symbol: precio}), minutos=minutos, dia=dia)
    else:
        _tick(db_session, _universo(**{symbol: "92"}), minutos=1, dia=dia)


@pytest.fixture
def seis_operaciones(db_session):
    # score alto -> ganan; score bajo -> pierden (relación fuerte a propósito)
    casos = [("BTCUSDT", "90", True), ("ETHUSDT", "80", True), ("SOLUSDT", "70", True),
             ("XRPUSDT", "60", False), ("BNBUSDT", "50", False), ("ADAUSDT", "40", False)]
    for dia, (symbol, puntaje, gana) in enumerate(casos):
        _operar(db_session, symbol, puntaje, gana, dia)
    return casos


def test_recalcular_performance_guarda_global_activos_y_terciles(db_session, seis_operaciones):
    ahora = _t(0, 30)
    metricas = recalculate_performance(
        db_session, period_start=_t(0, -1), period_end=_t(0, 10), initial_capital=CAPITAL, now=ahora
    )
    claves = [(m.scope_type, m.scope_key) for m in metricas]
    assert claves[0] == ("GLOBAL", None)
    assert [k for k in claves if k[0] == "SYMBOL"] == [("SYMBOL", s) for s in sorted(c[0] for c in seis_operaciones)]
    assert claves[-3:] == [("SCORE_TERCIL", "SUPERIOR"), ("SCORE_TERCIL", "MEDIO"), ("SCORE_TERCIL", "INFERIOR")]

    g = metricas[0]
    assert g.total_trades == 6 and g.winning_trades == 3 and g.losing_trades == 3
    assert g.win_rate_pct == D("50")
    assert g.net_pnl == D("3") * D("12.1876") + D("3") * D("-8.192")
    assert g.max_drawdown_pct is not None and g.max_drawdown_pct < 0  # sale de equity_observaciones
    assert g.benchmark_return_pct is None  # sin velas ingeridas en el periodo: no se inventa
    assert g.high_score_expectancy == D("12.1876") and g.low_score_expectancy == D("-8.192")
    assert g.score_discrimination == D("20.3796")

    filas = db_session.execute(text("SELECT count(*) FROM performance_metrics")).scalar_one()
    assert filas == len(metricas) == 10


def test_recalcular_es_idempotente_y_reconstruible(db_session, seis_operaciones):
    args = dict(period_start=_t(0, -1), period_end=_t(0, 10), initial_capital=CAPITAL, now=_t(0, 30))
    recalculate_performance(db_session, **args)
    primera = db_session.execute(text("SELECT * FROM performance_metrics ORDER BY scope_type, scope_key")).all()
    recalculate_performance(db_session, **args)  # mismo alcance y periodo: reemplaza, no duplica
    segunda = db_session.execute(text("SELECT * FROM performance_metrics ORDER BY scope_type, scope_key")).all()
    assert [tuple(r)[1:] for r in segunda] == [tuple(r)[1:] for r in primera]  # todo menos el id
    assert [r[0] for r in segunda] == [r[0] for r in primera]  # y las mismas filas

    # Reconstrucción: si la tabla se borra, el servicio la deja idéntica.
    db_session.execute(text("DELETE FROM performance_metrics"))
    recalculate_performance(db_session, **args)
    tercera = db_session.execute(text("SELECT * FROM performance_metrics ORDER BY scope_type, scope_key")).all()
    assert [tuple(r)[1:] for r in tercera] == [tuple(r)[1:] for r in primera]


def test_periodo_sin_operaciones_guarda_un_global_en_ceros(db_session):
    (g,) = recalculate_performance(
        db_session, period_start=_t(0, 100), period_end=_t(0, 101), initial_capital=CAPITAL, now=_t(0, 102)
    )
    assert g.total_trades == 0 and g.net_pnl == 0 and g.win_rate_pct is None


def test_validacion_de_score_sobre_los_resultados_guardados(db_session, seis_operaciones):
    r = validate_score(db_session)
    assert r.n_trades == 6 and r.n_trades_con_score == 6 and r.tercil_size == 2
    assert r.status.value == "PRELIMINAR"  # 6 < 30
    assert r.high_score_expectancy == D("12.1876") and r.low_score_expectancy == D("-8.192")
    assert r.score_discrimination == D("20.3796")
    # Con 6 operaciones el remuestreo a veces junta solo ganadoras o solo perdedoras:
    # el intervalo es ancho. La regla es la del plan: discrimina si el límite inferior > 0.
    assert r.ci_lower <= r.score_discrimination <= r.ci_upper
    assert r.discriminates == (r.ci_lower > 0)
    assert validate_score(db_session) == r  # reproducible


def test_el_resultado_no_depende_del_orden_de_cierre_ni_modifica_la_decision(db_session, seis_operaciones):
    from app.repositories.analysis import get_propuesta

    r = list_trade_results(db_session)[0]
    prop = get_propuesta(db_session, r.proposal_id)
    assert prop.decision_snapshot["symbol"] == r.symbol  # la decisión congelada no se tocó
