"""Tests del monitor (`app/execution/monitor.py`) y de la reanudación manual
(`app/execution/system_control.py`) contra Postgres real: equity, pico,
pausa por drawdown en el umbral exacto, reinicio con posición abierta,
reconciliación y tolerancia a fallos de precio/posición."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.domain.enums import (
    EstadoCiclo,
    EstadoOrden,
    EstadoPosicion,
    IntencionOrden,
    OrigenObservacionEquity,
    TierEntrada,
    TipoEventoSistema,
)
from app.domain.rules.execution import CandidatoEntrada
from app.execution import monitor as monitor_module
from app.execution.monitor import ReconciliationBlocked, run_monitor_tick
from app.execution.order_manager import open_entries
from app.execution.price_source import PriceUnavailable
from app.execution.system_control import ResultadoReanudacion, get_system_state, resume_system
from app.repositories.analysis import create_ciclo, save_propuesta
from app.repositories.equity import (
    EquityObservacionRow,
    append_observacion,
    get_ultima_observacion,
    list_observaciones,
)
from app.repositories.orders import list_orders
from app.repositories.positions import get_posicion, list_open_posiciones
from app.repositories.system_events import get_ultimo_evento_sistema, list_eventos_sistema
from tests.integration.test_analysis_repository import _propuesta_row

D = Decimal
CAPITAL = D("1000")
_AHORA = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)


def _t(minutos: int) -> datetime:
    return _AHORA + timedelta(minutes=minutos)


class FakePrices:
    def __init__(self, precios: dict[str, str] | None = None):
        self.precios = {k: D(v) for k, v in (precios or {}).items()}

    def get_price(self, symbol: str) -> Decimal:
        if symbol not in self.precios:
            raise PriceUnavailable(symbol)
        return self.precios[symbol]


def _abrir(
    db_session, symbol="BTCUSDT", precio="100", tier=TierEntrada.NORMAL, puntaje="64.60", now=_AHORA, con_aviso=False
):
    """Abre una posición. El aviso `POSICION_ABIERTA` que deja la apertura se borra
    por defecto: estas pruebas miden los avisos del MONITOR, no el de la apertura
    (ese tiene sus propias pruebas en `test_position_opened_alert.py`)."""
    ciclo = create_ciclo(db_session, started_at=now, status=EstadoCiclo.PREFILTER)
    row = dataclasses.replace(_propuesta_row(ciclo, symbol), tier_entrada=tier, puntaje_ponderado=D(puntaje))
    pid = save_propuesta(db_session, row)
    cand = CandidatoEntrada(pid, symbol, tier, D(puntaje))
    if con_aviso:
        open_entries(db_session, [cand], now=now, price_source=FakePrices({symbol: precio}))
    else:  # `alertas` es de solo-agregar: no se puede borrar el aviso, así que no se escribe
        with patch("app.execution.order_manager._avisar_posicion_abierta"):
            open_entries(db_session, [cand], now=now, price_source=FakePrices({symbol: precio}))
    return pid


def _tick(db_session, precios, minutos=1):
    return run_monitor_tick(
        db_session, now=_t(minutos), price_source=FakePrices(precios), initial_capital=CAPITAL
    )


def _sembrar_pico(db_session, equity, peak):
    append_observacion(
        db_session,
        EquityObservacionRow(
            observed_at=_AHORA - timedelta(hours=1),
            origen=OrigenObservacionEquity.MONITOR,
            equity_usd=D(equity),
            peak_equity_usd=D(peak),
            open_positions=0,
        ),
    )


# --- equity y pico ---


def test_sin_posiciones_el_equity_es_el_capital_y_queda_registrado(db_session):
    res = _tick(db_session, {})
    assert res.equity == D("1000") and res.peak_equity == D("1000") and res.drawdown_pct == 0
    assert res.posiciones_abiertas == 0 and res.pausa_registrada is False
    ultima = get_ultima_observacion(db_session)
    assert (ultima.equity_usd, ultima.peak_equity_usd, ultima.origen) == (
        D("1000"),
        D("1000"),
        OrigenObservacionEquity.MONITOR,
    )


def test_posicion_sin_movimiento_solo_pierde_la_comision_de_entrada(db_session):
    _abrir(db_session)
    res = _tick(db_session, {"BTCUSDT": "100"})
    assert res.equity == D("999.9")
    assert res.peak_equity == D("1000")  # el pico inicial es el capital
    assert res.posiciones_abiertas == 1 and res.chequeos[0].orden_id is None


def test_el_pico_solo_sube_aunque_el_equity_baje(db_session):
    _abrir(db_session)
    _tick(db_session, {"BTCUSDT": "110"}, 1)  # equity 1009.9, pico 1009.9
    res = _tick(db_session, {"BTCUSDT": "105"}, 2)  # equity 1004.9
    assert res.equity == D("1004.9") and res.peak_equity == D("1009.9")
    assert [o.peak_equity_usd for o in list_observaciones(db_session)] == [D("1009.9"), D("1009.9")]


def test_el_pico_vigente_sobrevive_a_un_reinicio(db_session):
    _abrir(db_session)
    _tick(db_session, {"BTCUSDT": "110"}, 1)
    # "Reinicio": nada vive en memoria; el siguiente chequeo lee el pico de la base.
    res = _tick(db_session, {"BTCUSDT": "100"}, 2)
    assert res.peak_equity == D("1009.9")


def test_un_stop_ejecutado_en_el_chequeo_se_refleja_en_el_equity(db_session):
    _abrir(db_session)
    res = _tick(db_session, {"BTCUSDT": "92"})
    assert [c.intencion for c in res.chequeos] == [IntencionOrden.STOP_LOSS]
    # 1000 - 100,1 (compra) + 92 - 0,092 (venta) = 991,808
    assert res.equity == D("991.808") and res.posiciones_abiertas == 0
    assert list_open_posiciones(db_session) == []


def test_now_sin_zona_horaria_se_rechaza(db_session):
    with pytest.raises(ValueError):
        run_monitor_tick(
            db_session, now=datetime(2026, 10, 5, 15, 0), price_source=FakePrices(), initial_capital=CAPITAL
        )


# --- pausa por drawdown en el umbral exacto ---


def test_drawdown_exactamente_en_menos_15_por_ciento_registra_la_pausa(db_session):
    _abrir(db_session)
    _sembrar_pico(db_session, "1170", "1170")  # umbral: 1170 × 0,85 = 994,5
    res = _tick(db_session, {"BTCUSDT": "94.6"})  # equity: 899,9 + 94,6 = 994,5

    assert res.equity == D("994.5") and res.peak_equity == D("1170")
    assert res.pausa_registrada is True
    (evento,) = list_eventos_sistema(db_session)
    assert evento.event_type == TipoEventoSistema.PAUSA_DRAWDOWN
    assert (evento.equity_usd, evento.peak_equity_usd) == (D("994.5"), D("1170"))
    assert "-15" in evento.detail


def test_un_centavo_antes_del_umbral_no_pausa(db_session):
    _abrir(db_session)
    _sembrar_pico(db_session, "1170", "1170")
    res = _tick(db_session, {"BTCUSDT": "94.61"})  # equity 994,51
    assert res.pausa_registrada is False
    assert list_eventos_sistema(db_session) == []


def test_la_pausa_no_se_duplica_y_el_monitor_sigue_vigilando(db_session):
    _abrir(db_session)
    _sembrar_pico(db_session, "1170", "1170")
    _tick(db_session, {"BTCUSDT": "94.6"}, 1)
    res = _tick(db_session, {"BTCUSDT": "94.6"}, 2)

    assert res.pausa_registrada is False
    assert len(list_eventos_sistema(db_session)) == 1
    assert len(list_observaciones(db_session)) == 3  # la sembrada + 2 chequeos: siguen observando
    # Pausado, pero el stop sigue activo:
    res = _tick(db_session, {"BTCUSDT": "90"}, 3)
    assert [c.intencion for c in res.chequeos] == [IntencionOrden.STOP_LOSS]


# --- reanudación manual ---


def _pausar(db_session):
    _abrir(db_session)
    _sembrar_pico(db_session, "1170", "1170")
    _tick(db_session, {"BTCUSDT": "94.6"}, 1)


def test_reanudar_reinicia_el_pico_al_equity_actual_y_no_vuelve_a_pausar(db_session):
    _pausar(db_session)
    resultado, equity = resume_system(
        db_session, now=_t(5), price_source=FakePrices({"BTCUSDT": "94.6"}), initial_capital=CAPITAL
    )
    assert (resultado, equity) == (ResultadoReanudacion.REANUDADO, D("994.5"))

    ultimo = get_ultimo_evento_sistema(db_session)
    assert ultimo.event_type == TipoEventoSistema.REANUDACION
    assert ultimo.peak_equity_usd == D("994.5") and ultimo.previous_peak_equity_usd == D("1170")
    obs = get_ultima_observacion(db_session)
    assert (obs.origen, obs.equity_usd, obs.peak_equity_usd) == (
        OrigenObservacionEquity.REANUDACION,
        D("994.5"),
        D("994.5"),
    )

    # El siguiente chequeo parte del pico nuevo: no re-pausa al instante.
    res = _tick(db_session, {"BTCUSDT": "94.6"}, 6)
    assert res.peak_equity == D("994.5") and res.pausa_registrada is False


def test_reanudar_sin_estar_pausado_no_hace_nada(db_session):
    _abrir(db_session)
    resultado, equity = resume_system(
        db_session, now=_t(5), price_source=FakePrices({"BTCUSDT": "100"}), initial_capital=CAPITAL
    )
    assert (resultado, equity) == (ResultadoReanudacion.NO_PAUSADO, None)
    assert list_eventos_sistema(db_session) == [] and list_observaciones(db_session) == []


def test_reanudar_sin_precio_no_reanuda_ni_escribe_nada(db_session):
    _pausar(db_session)
    eventos, observaciones = len(list_eventos_sistema(db_session)), len(list_observaciones(db_session))
    resultado, equity = resume_system(
        db_session, now=_t(5), price_source=FakePrices({}), initial_capital=CAPITAL
    )
    assert (resultado, equity) == (ResultadoReanudacion.SIN_PRECIO, None)
    assert len(list_eventos_sistema(db_session)) == eventos
    assert len(list_observaciones(db_session)) == observaciones
    assert get_ultimo_evento_sistema(db_session).event_type == TipoEventoSistema.PAUSA_DRAWDOWN


def test_se_puede_pausar_de_nuevo_despues_de_reanudar(db_session):
    _pausar(db_session)
    resume_system(db_session, now=_t(5), price_source=FakePrices({"BTCUSDT": "94.6"}), initial_capital=CAPITAL)
    # Pico nuevo 994,5 -> umbral 845,325: se cruza con otra posición cayendo mucho.
    _sembrar_pico(db_session, "1200", "1200")
    res = _tick(db_session, {"BTCUSDT": "94.6"}, 10)  # equity 994,5; pico 1200 -> umbral 1020: pausa
    assert res.pausa_registrada is True
    assert [e.event_type for e in list_eventos_sistema(db_session)] == [
        TipoEventoSistema.PAUSA_DRAWDOWN,
        TipoEventoSistema.REANUDACION,
        TipoEventoSistema.PAUSA_DRAWDOWN,
    ]


def test_estado_del_sistema(db_session):
    estado = get_system_state(db_session, initial_capital=CAPITAL)
    assert (estado.pausado, estado.parada_emergencia, estado.posiciones_abiertas) == (False, False, 0)
    assert estado.ultimo_equity is None and estado.pico_vigente == CAPITAL

    _pausar(db_session)
    estado = get_system_state(db_session, initial_capital=CAPITAL)
    assert estado.pausado is True and estado.posiciones_abiertas == 1
    assert estado.ultimo_equity == D("994.5") and estado.pico_vigente == D("1170")


# --- reinicio con posición abierta ---


def test_reinicio_con_posicion_abierta_y_precio_normal_no_cierra_nada(db_session):
    _abrir(db_session)
    # Pasaron 3 días con el monitor apagado; el precio está cerca de la entrada.
    res = run_monitor_tick(
        db_session,
        now=_AHORA + timedelta(days=3),
        price_source=FakePrices({"BTCUSDT": "101"}),
        initial_capital=CAPITAL,
    )
    assert [c.accion.value for c in res.chequeos] == ["MANTENER"]
    assert len(list_open_posiciones(db_session)) == 1
    assert [o.intencion for o in list_orders(db_session, estados=[EstadoOrden.FILLED])] == [IntencionOrden.ENTRY]


def test_reinicio_con_el_precio_ya_bajo_el_stop_sale_de_inmediato_al_precio_actual(db_session):
    _abrir(db_session)
    res = run_monitor_tick(
        db_session,
        now=_AHORA + timedelta(days=3),
        price_source=FakePrices({"BTCUSDT": "70"}),  # cayó mientras estuvo apagado
        initial_capital=CAPITAL,
    )
    assert [c.intencion for c in res.chequeos] == [IntencionOrden.STOP_LOSS]
    venta = [o for o in list_orders(db_session, estados=[EstadoOrden.FILLED]) if o.intencion == IntencionOrden.STOP_LOSS][0]
    assert venta.execution_price == D("70")  # precio de ahora, no el nivel de 92


def test_reinicio_con_el_precio_ya_sobre_el_objetivo_toma_la_ganancia_parcial(db_session):
    _abrir(db_session)
    res = run_monitor_tick(
        db_session,
        now=_AHORA + timedelta(days=3),
        price_source=FakePrices({"BTCUSDT": "130"}),
        initial_capital=CAPITAL,
    )
    assert [c.intencion for c in res.chequeos] == [IntencionOrden.TAKE_PROFIT_PARTIAL]
    pos = list_open_posiciones(db_session)[0]
    assert pos.remaining_quantity == D("0.5") and pos.trailing_peak_price == D("130")


def test_reintentar_el_mismo_chequeo_no_duplica_ordenes(db_session):
    _abrir(db_session)
    _tick(db_session, {"BTCUSDT": "92"}, 1)
    _tick(db_session, {"BTCUSDT": "92"}, 1)  # reintento tras una caída: no hay posición abierta
    ventas = [o for o in list_orders(db_session, estados=[EstadoOrden.FILLED]) if o.side.value == "SELL"]
    assert len(ventas) == 1


# --- precios y errores ---


def test_sin_precio_de_una_posicion_se_evalua_el_resto_y_no_se_inventa_equity(db_session):
    _abrir(db_session, "BTCUSDT")
    _abrir(db_session, "ETHUSDT")
    res = _tick(db_session, {"ETHUSDT": "90"})  # BTC sin precio; ETH cayó bajo el stop

    assert res.sin_precio == ("BTCUSDT",)
    assert [c.symbol for c in res.chequeos] == ["ETHUSDT"] and res.chequeos[0].intencion == IntencionOrden.STOP_LOSS
    assert res.equity is None and res.hubo_problemas is True
    assert list_observaciones(db_session) == []  # no se escribió equity a medias
    assert [p.symbol for p in list_open_posiciones(db_session)] == ["BTCUSDT"]


def test_el_error_de_una_posicion_no_deshace_las_demas_ni_escribe_equity(db_session, monkeypatch):
    _abrir(db_session, "BTCUSDT")
    _abrir(db_session, "ETHUSDT")
    real = monitor_module.check_position

    def falla_en_btc(session, posicion, precio, *, now):
        if posicion.symbol == "BTCUSDT":
            session.execute(text("SELECT 1"))
            raise RuntimeError("fallo simulado")
        return real(session, posicion, precio, now=now)

    monkeypatch.setattr(monitor_module, "check_position", falla_en_btc)
    res = _tick(db_session, {"BTCUSDT": "100", "ETHUSDT": "90"})

    assert [e.symbol for e in res.errores] == ["BTCUSDT"] and "fallo simulado" in res.errores[0].mensaje
    assert [c.symbol for c in res.chequeos] == ["ETHUSDT"]  # la otra sí se procesó
    assert res.equity is None and list_observaciones(db_session) == []


# --- PARAR no apaga los stops ---


def test_el_archivo_parar_no_detiene_la_vigilancia_de_las_posiciones(db_session, tmp_path):
    from app.execution import emergency_stop

    _abrir(db_session)
    emergency_stop.PARAR_PATH.write_text("")  # PARAR presente
    res = _tick(db_session, {"BTCUSDT": "92"})
    assert [c.intencion for c in res.chequeos] == [IntencionOrden.STOP_LOSS]


# --- entradas pendientes ---


def test_el_chequeo_expira_las_entradas_pendientes_vencidas(db_session):
    _abrir(db_session, "ETHUSDT", tier=TierEntrada.MAYOR_RIESGO, puntaje="52")  # queda PENDING_APPROVAL
    res = run_monitor_tick(
        db_session, now=_AHORA + timedelta(days=1), price_source=FakePrices(), initial_capital=CAPITAL
    )
    assert res.expiradas == 1
    assert list_orders(db_session, estados=[EstadoOrden.EXPIRED])[0].symbol == "ETHUSDT"


# --- reconciliación ---


def test_una_discrepancia_bloquea_el_chequeo_sin_tocar_nada(db_session):
    _abrir(db_session)
    pos = list_open_posiciones(db_session)[0]
    # Corrupción simulada: la cantidad restante ya no se explica con las órdenes.
    db_session.execute(
        text("UPDATE posiciones SET remaining_quantity = 0.5 WHERE id = :id"), {"id": pos.id}
    )
    db_session.expire_all()

    with pytest.raises(ReconciliationBlocked) as info:
        _tick(db_session, {"BTCUSDT": "50"})  # a este precio habría vendido todo
    assert [d.codigo.value for d in info.value.discrepancias] == ["CANTIDAD_RESTANTE_NO_COINCIDE"]
    assert [o.intencion for o in list_orders(db_session, estados=[EstadoOrden.FILLED])] == [IntencionOrden.ENTRY]
    assert list_observaciones(db_session) == []
    assert get_posicion(db_session, pos.id).status == EstadoPosicion.OPEN


def test_el_estado_sano_pasa_la_reconciliacion_durante_toda_la_vida_de_una_posicion(db_session):
    _abrir(db_session)
    _tick(db_session, {"BTCUSDT": "112"}, 1)  # TP parcial
    _tick(db_session, {"BTCUSDT": "120"}, 2)  # sube el máximo
    res = _tick(db_session, {"BTCUSDT": "112.8"}, 3)  # trailing cierra el resto
    assert [c.intencion for c in res.chequeos] == [IntencionOrden.TRAILING_STOP]
    _tick(db_session, {}, 4)  # ya cerrada: sigue cuadrando
