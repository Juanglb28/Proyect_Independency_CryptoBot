"""Escalamientos del monitor (Paso 11a-2) contra Postgres real: qué filas de
`alertas` escribe un chequeo, que no se repitan, que avisar nunca bloquee
operar y que "Binance caído" bloquee entradas nuevas."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

import app.execution.escalations as esc
from app.data.exchange_client import Candle
from app.domain.enums import EstadoCiclo, EstadoSnapshot, TierEntrada, TipoAlerta
from app.domain.rules.execution import CandidatoEntrada
from app.execution.escalations import avisar_discrepancia
from app.execution.monitor import ReconciliationBlocked, run_monitor_tick
from app.execution.order_manager import build_cupos, open_entries
from app.execution.reconciliation import CodigoDiscrepancia, Discrepancia
from app.repositories.alertas import list_alertas
from app.repositories.analysis import create_ciclo, save_propuesta
from app.repositories.candles import save_candles
from app.repositories.orders import list_orders
from app.repositories.positions import list_open_posiciones
from app.repositories.snapshots import list_snapshots_for_position
from tests.integration.test_analysis_repository import _propuesta_row
from tests.integration.test_monitor import CAPITAL, FakePrices, _abrir, _sembrar_pico, _t

D = Decimal


def _tick(db_session, precios, minutos):
    return run_monitor_tick(
        db_session, now=_t(minutos), price_source=FakePrices(precios), initial_capital=CAPITAL
    )


def _tipos(db_session) -> list[TipoAlerta]:
    return [a.tipo for a in reversed(list_alertas(db_session))]


# --- caída en 1 hora ---


def test_caida_de_mas_de_5_por_ciento_en_una_hora_avisa_una_vez_por_hora(db_session):
    _abrir(db_session)
    _tick(db_session, {"BTCUSDT": "100"}, 1)  # snapshot de referencia (precio 100)
    res = _tick(db_session, {"BTCUSDT": "94.9"}, 61)  # -5,1 % respecto de hace 1 h; el stop está en 92
    assert res.alertas_creadas == (TipoAlerta.CAIDA_1H,)

    (alerta,) = list_alertas(db_session)
    assert (alerta.tipo, alerta.subject, alerta.observed_value) == (TipoAlerta.CAIDA_1H, "BTCUSDT", D("5.1"))
    assert alerta.position_id == list_open_posiciones(db_session)[0].id
    assert "BTCUSDT" in alerta.message and "5.10" in alerta.message

    # El mismo chequeo repetido (reintento / reinicio) y otro de la misma hora no duplican.
    assert _tick(db_session, {"BTCUSDT": "94.9"}, 61).alertas_creadas == ()
    assert _tick(db_session, {"BTCUSDT": "94.5"}, 62).alertas_creadas == ()
    assert len(list_alertas(db_session)) == 1


def test_el_aviso_de_caida_deja_un_snapshot_risk_alert(db_session):
    _abrir(db_session)
    _tick(db_session, {"BTCUSDT": "100"}, 1)
    _tick(db_session, {"BTCUSDT": "94.9"}, 61)
    snaps = list_snapshots_for_position(db_session, list_open_posiciones(db_session)[0].id)
    riesgo = [s for s in snaps if s.system_state == EstadoSnapshot.RISK_ALERT]
    assert len(riesgo) == 1 and riesgo[0].market_price == D("94.9") and riesgo[0].snapshot_time == _t(61)


def test_exactamente_5_por_ciento_no_avisa(db_session):
    _abrir(db_session)
    _tick(db_session, {"BTCUSDT": "100"}, 1)
    res = _tick(db_session, {"BTCUSDT": "95"}, 61)
    assert res.alertas_creadas == () and list_alertas(db_session) == []


def test_sin_referencia_de_hace_una_hora_no_se_avisa_ni_se_estima(db_session):
    _abrir(db_session)
    res = _tick(db_session, {"BTCUSDT": "90"}, 30)  # abierta hace 30 min: no hay snapshot de hace 1 h
    assert res.alertas_creadas == () and list_alertas(db_session) == []


def test_tras_un_hueco_de_datos_la_referencia_vieja_no_sirve(db_session):
    _abrir(db_session)
    _tick(db_session, {"BTCUSDT": "100"}, 1)
    # El monitor estuvo apagado 3 horas: el snapshot más cercano a "hace 1 h" tiene 3 h.
    res = _tick(db_session, {"BTCUSDT": "94"}, 180)
    assert res.alertas_creadas == ()


def test_una_posicion_que_se_cierra_en_el_chequeo_no_avisa_de_caida(db_session):
    _abrir(db_session)
    _tick(db_session, {"BTCUSDT": "100"}, 1)
    res = _tick(db_session, {"BTCUSDT": "90"}, 61)  # cae más de 5 % pero cruza el stop y se cierra
    assert res.posiciones_abiertas == 0 and res.alertas_creadas == ()


# --- volumen atípico ---


def _velas_1h(symbol, ultima_apertura, volumenes):
    """Velas de 1 h que terminan en `ultima_apertura`; `volumenes` de la más antigua a la más reciente."""
    n = len(volumenes)
    return [
        Candle(
            symbol=symbol,
            timeframe="1h",
            open_time=ultima_apertura - timedelta(hours=n - 1 - i),
            open=D(100),
            high=D(101),
            low=D(99),
            close=D(100),
            volume=D(v),
        )
        for i, v in enumerate(volumenes)
    ]


# En _t(61) = 16:01 UTC la última vela 1h cerrada es la que abrió a las 15:00.
_VELA_OBJETIVO = _t(0)
_NORMALES = [10 + (i % 3) for i in range(30)]


def test_volumen_a_mas_de_3_desviaciones_avisa_y_deja_snapshot(db_session):
    _abrir(db_session)
    save_candles(db_session, _velas_1h("BTCUSDT", _VELA_OBJETIVO, _NORMALES + [500]))
    res = _tick(db_session, {"BTCUSDT": "100"}, 61)
    assert res.alertas_creadas == (TipoAlerta.VOLUMEN_ATIPICO,)

    (alerta,) = list_alertas(db_session)
    assert alerta.tipo == TipoAlerta.VOLUMEN_ATIPICO and alerta.observed_value > 3
    assert "500.00 BTC" in alerta.message  # volumen de la vela, con su unidad
    assert "Vela anterior" in alerta.message and "Promedio de las 30 velas previas" in alerta.message
    # Un aviso por vela: el siguiente chequeo (misma vela) no repite.
    assert _tick(db_session, {"BTCUSDT": "100"}, 62).alertas_creadas == ()
    snaps = list_snapshots_for_position(db_session, list_open_posiciones(db_session)[0].id)
    assert sum(1 for s in snaps if s.system_state == EstadoSnapshot.RISK_ALERT) == 1


def test_volumen_normal_no_avisa(db_session):
    _abrir(db_session)
    save_candles(db_session, _velas_1h("BTCUSDT", _VELA_OBJETIVO, _NORMALES + [11]))
    assert _tick(db_session, {"BTCUSDT": "100"}, 61).alertas_creadas == ()


def test_si_falta_una_vela_de_referencia_no_se_avisa(db_session):
    _abrir(db_session)
    velas = _velas_1h("BTCUSDT", _VELA_OBJETIVO, _NORMALES + [500])
    del velas[10]  # un hueco en la serie
    save_candles(db_session, velas)
    assert _tick(db_session, {"BTCUSDT": "100"}, 61).alertas_creadas == ()


def test_si_la_ultima_vela_cerrada_aun_no_esta_guardada_no_se_avisa(db_session):
    _abrir(db_session)
    save_candles(db_session, _velas_1h("BTCUSDT", _VELA_OBJETIVO - timedelta(hours=1), _NORMALES + [500]))
    assert _tick(db_session, {"BTCUSDT": "100"}, 61).alertas_creadas == ()


def test_volumen_sin_variacion_no_tiene_zscore_y_no_avisa(db_session):
    _abrir(db_session)
    save_candles(db_session, _velas_1h("BTCUSDT", _VELA_OBJETIVO, [10] * 30 + [500]))
    assert _tick(db_session, {"BTCUSDT": "100"}, 61).alertas_creadas == ()


def test_el_volumen_de_otro_simbolo_sin_posicion_no_avisa(db_session):
    _abrir(db_session)
    save_candles(db_session, _velas_1h("ETHUSDT", _VELA_OBJETIVO, _NORMALES + [500]))
    assert _tick(db_session, {"BTCUSDT": "100"}, 61).alertas_creadas == ()


# --- entradas pendientes de aprobación ---


def _entrada_pendiente(db_session, symbol="ETHUSDT", now=_t(0)):
    ciclo = create_ciclo(db_session, started_at=now, status=EstadoCiclo.PREFILTER)
    row = dataclasses.replace(
        _propuesta_row(ciclo, symbol), tier_entrada=TierEntrada.MAYOR_RIESGO, puntaje_ponderado=D("52")
    )
    pid = save_propuesta(db_session, row)
    open_entries(
        db_session,
        [CandidatoEntrada(pid, symbol, TierEntrada.MAYOR_RIESGO, D("52"))],
        now=now,
        price_source=FakePrices({symbol: "10"}),
    )


def test_una_entrada_que_espera_aprobacion_avisa_una_sola_vez(db_session):
    _entrada_pendiente(db_session)
    (orden,) = list_orders(db_session, estados=["PENDING_APPROVAL"])
    assert _tick(db_session, {}, 1).alertas_creadas == (TipoAlerta.ORDEN_PENDIENTE_APROBACION,)
    assert _tick(db_session, {}, 2).alertas_creadas == ()  # el aviso es por orden, no por chequeo

    (alerta,) = list_alertas(db_session)
    assert (alerta.order_id, alerta.subject, alerta.observed_value) == (orden.id, "ETHUSDT", orden.requested_usd)
    assert "aprobación" in alerta.message


def test_sin_entradas_pendientes_no_hay_aviso_de_orden(db_session):
    assert _tick(db_session, {}, 1).alertas_creadas == ()


# --- Binance caído / restablecido ---


def test_binance_caido_tras_mas_de_5_minutos_sin_precio_y_restablecido_al_volver(db_session):
    _abrir(db_session)
    _tick(db_session, {"BTCUSDT": "100"}, 1)  # último precio recibido: minuto 1
    assert _tick(db_session, {}, 3).alertas_creadas == ()  # 2 min: todavía no
    assert _tick(db_session, {}, 6).alertas_creadas == ()  # exactamente 5 min: no es "más de 5"

    res = _tick(db_session, {}, 7)  # 6 min
    assert res.alertas_creadas == (TipoAlerta.BINANCE_CAIDO,) and res.hubo_problemas
    assert _tick(db_session, {}, 8).alertas_creadas == ()  # no repite mientras siga caído

    assert _tick(db_session, {"BTCUSDT": "100"}, 9).alertas_creadas == (TipoAlerta.BINANCE_RESTABLECIDO,)
    assert _tick(db_session, {"BTCUSDT": "100"}, 10).alertas_creadas == ()
    assert _tipos(db_session) == [TipoAlerta.BINANCE_CAIDO, TipoAlerta.BINANCE_RESTABLECIDO]


def test_con_dos_posiciones_un_precio_que_falta_no_es_binance_caido(db_session):
    _abrir(db_session)
    _abrir(db_session, symbol="ETHUSDT", precio="10")
    _tick(db_session, {"BTCUSDT": "100", "ETHUSDT": "10"}, 1)
    res = _tick(db_session, {"BTCUSDT": "100"}, 30)  # solo falla ETHUSDT
    assert res.sin_precio == ("ETHUSDT",) and res.alertas_creadas == ()


def test_sin_posiciones_abiertas_el_monitor_no_puede_saber_si_binance_cayo(db_session):
    assert _tick(db_session, {}, 60).alertas_creadas == ()


def test_mientras_binance_figura_caido_no_se_abren_entradas_nuevas(db_session):
    _abrir(db_session)
    _tick(db_session, {"BTCUSDT": "100"}, 1)
    _tick(db_session, {}, 7)  # BINANCE_CAIDO
    assert build_cupos(db_session, _t(8)).binance_caido is True

    ciclo = create_ciclo(db_session, started_at=_t(8), status=EstadoCiclo.PREFILTER)
    pid = save_propuesta(db_session, dataclasses.replace(_propuesta_row(ciclo, "ETHUSDT"), tier_entrada=TierEntrada.NORMAL))
    res = open_entries(
        db_session,
        [CandidatoEntrada(pid, "ETHUSDT", TierEntrada.NORMAL, D("64.6"))],
        now=_t(8),
        price_source=FakePrices({"ETHUSDT": "10"}),
    )
    assert res.procesadas == () and [m.value for m in res.descartadas[0].motivos] == ["BINANCE_CAIDO"]

    _tick(db_session, {"BTCUSDT": "100"}, 9)  # RESTABLECIDO
    assert build_cupos(db_session, _t(10)).binance_caido is False


def test_mientras_binance_figura_caido_el_monitor_sigue_aplicando_stops_al_volver_el_precio(db_session):
    _abrir(db_session)
    _tick(db_session, {"BTCUSDT": "100"}, 1)
    _tick(db_session, {}, 7)
    res = _tick(db_session, {"BTCUSDT": "90"}, 9)  # vuelve el precio, ya bajo el stop
    assert [c.intencion.value for c in res.chequeos] == ["STOP_LOSS"]
    assert TipoAlerta.BINANCE_RESTABLECIDO in res.alertas_creadas


# --- pausa por drawdown ---


def test_la_pausa_por_drawdown_avisa_una_sola_vez(db_session):
    _abrir(db_session)
    _sembrar_pico(db_session, "1170", "1170")
    res = _tick(db_session, {"BTCUSDT": "94.6"}, 1)
    assert res.pausa_registrada and TipoAlerta.PAUSA_DRAWDOWN in res.alertas_creadas
    assert _tick(db_session, {"BTCUSDT": "94.6"}, 2).alertas_creadas == ()  # ya pausado

    (alerta,) = [a for a in list_alertas(db_session) if a.tipo == TipoAlerta.PAUSA_DRAWDOWN]
    assert alerta.subject == "SISTEMA" and alerta.observed_value < D("-15") + D("1") and "-15 %" in alerta.message


# --- discrepancia de reconciliación ---


def test_la_discrepancia_se_avisa_una_vez_por_hora_y_conjunto_de_hallazgos(db_session):
    d = [Discrepancia(CodigoDiscrepancia.SIN_ORDEN_DE_ENTRADA, "posición X sin orden")]
    assert avisar_discrepancia(db_session, now=_t(1), discrepancias=d) is True
    assert avisar_discrepancia(db_session, now=_t(2), discrepancias=d) is False  # misma hora
    otra = d + [Discrepancia(CodigoDiscrepancia.ENTRADAS_MULTIPLES, "dos entradas")]
    assert avisar_discrepancia(db_session, now=_t(3), discrepancias=otra) is True  # otro conjunto
    assert avisar_discrepancia(db_session, now=_t(61), discrepancias=d) is True  # hora siguiente
    alertas = list_alertas(db_session)
    assert all(a.tipo == TipoAlerta.DISCREPANCIA_RECONCILIACION for a in alertas) and len(alertas) == 3
    assert "SIN_ORDEN_DE_ENTRADA" in alertas[-1].message


# --- avisar nunca bloquea operar ---


def test_si_una_comprobacion_falla_el_monitor_sigue_y_las_demas_corren(db_session, monkeypatch):
    def _roto(*_args, **_kwargs):
        raise RuntimeError("falla simulada")

    monkeypatch.setattr(esc, "_caidas", _roto)
    _entrada_pendiente(db_session)
    _abrir(db_session)
    res = _tick(db_session, {"BTCUSDT": "100"}, 1)
    assert res.equity is not None and res.errores == ()  # el monitor completó su trabajo
    assert res.alertas_creadas == (TipoAlerta.ORDEN_PENDIENTE_APROBACION,)  # y las otras comprobaciones corrieron


def test_un_fallo_de_la_tabla_alertas_no_impide_ejecutar_un_stop(db_session):
    _abrir(db_session)
    db_session.execute(text("ALTER TABLE alertas RENAME TO alertas_rota"))
    try:
        res = _tick(db_session, {"BTCUSDT": "90"}, 1)
        assert [c.intencion.value for c in res.chequeos] == ["STOP_LOSS"]
        assert res.posiciones_abiertas == 0 and res.alertas_creadas == ()
    finally:
        db_session.execute(text("ALTER TABLE alertas_rota RENAME TO alertas"))


def test_la_discrepancia_sigue_bloqueando_el_chequeo(db_session):
    _abrir(db_session)
    pos = list_open_posiciones(db_session)[0]
    db_session.execute(text("UPDATE posiciones SET remaining_quantity = 0.5 WHERE id = :id"), {"id": pos.id})
    db_session.expire_all()
    with pytest.raises(ReconciliationBlocked):
        _tick(db_session, {"BTCUSDT": "50"}, 1)
    assert list_alertas(db_session) == []  # el aviso lo escribe quien captura la excepción, no el chequeo
