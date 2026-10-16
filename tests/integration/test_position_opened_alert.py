"""Aviso de Telegram al abrir una posición paper (2026-10-12): una fila
`POSICION_ABIERTA` por posición, en las tres vías de apertura (estrategia
automática, observación, aprobación manual), idempotente y que nunca bloquea
una entrada."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.domain.enums import TierEntrada, TipoAlerta
from app.execution import order_manager as om
from app.execution.order_manager import approve_pending_entry, open_entries
from app.repositories.alertas import list_alertas
from app.repositories.positions import count_open_posiciones, list_open_posiciones
from tests.integration.test_order_manager import _AHORA, FakePrices, _candidato, _ciclo


def _avisos(db_session):
    return [a for a in list_alertas(db_session) if a.tipo == TipoAlerta.POSICION_ABIERTA]


def test_una_entrada_automatica_deja_un_aviso_con_las_cifras_guardadas(db_session):
    cand = _candidato(db_session, _ciclo(db_session), "BTCUSDT")
    open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({"BTCUSDT": "100"}))

    (aviso,) = _avisos(db_session)
    pos = list_open_posiciones(db_session)[0]
    assert (aviso.subject, aviso.position_id, aviso.occurred_at) == ("BTCUSDT", pos.id, _AHORA)
    assert aviso.dedup_key == f"posicion-abierta:{pos.id}"
    assert aviso.order_id is not None
    assert aviso.observed_value == Decimal("100")  # 1 BTC a 100 = 100 USD
    for texto in ("POSICIÓN PAPER ABIERTA BTCUSDT", "estrategia", "Total de compra: 100.00 USD", "Stop 92", "objetivo 112"):
        assert texto in aviso.message
    assert "Simulado" in aviso.message


def test_una_observacion_se_rotula_sin_ia(db_session):
    cand = _candidato(db_session, _ciclo(db_session), "SOLUSDT", TierEntrada.OBSERVACION, "47")
    open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({"SOLUSDT": "50"}))

    (aviso,) = _avisos(db_session)
    assert "observación, sin IA" in aviso.message and "Puntaje del pre-filtro" in aviso.message


def test_la_aprobacion_manual_avisa_al_ejecutarse_y_no_antes(db_session):
    cand = _candidato(db_session, _ciclo(db_session), "ETHUSDT", TierEntrada.MAYOR_RIESGO, "52")
    orden_id = open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({})).procesadas[0].orden_id
    assert _avisos(db_session) == []  # pendiente de aprobación: todavía no hay posición

    ahora = _AHORA + timedelta(hours=2)
    approve_pending_entry(db_session, orden_id, now=ahora, price_source=FakePrices({"ETHUSDT": "200"}))
    approve_pending_entry(db_session, orden_id, now=ahora, price_source=FakePrices({"ETHUSDT": "200"}))  # repetida

    (aviso,) = _avisos(db_session)
    assert aviso.subject == "ETHUSDT" and aviso.occurred_at == ahora and aviso.order_id == orden_id
    assert "aprobada por ti" in aviso.message and "Compra: 0.500000 ETH a 200" in aviso.message


def test_reintentar_la_entrada_no_duplica_el_aviso(db_session):
    cand = _candidato(db_session, _ciclo(db_session), "BTCUSDT")
    precios = FakePrices({"BTCUSDT": "100"})
    open_entries(db_session, [cand], now=_AHORA, price_source=precios)
    open_entries(db_session, [cand], now=_AHORA, price_source=precios)
    assert len(_avisos(db_session)) == 1


def test_si_el_aviso_falla_la_posicion_se_abre_igual(db_session, monkeypatch):
    def _roto(*args, **kwargs):
        raise RuntimeError("fallo simulado")

    monkeypatch.setattr(om, "append_alerta", _roto)
    cand = _candidato(db_session, _ciclo(db_session), "BTCUSDT")
    open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({"BTCUSDT": "100"}))

    assert count_open_posiciones(db_session) == 1
    assert _avisos(db_session) == []
