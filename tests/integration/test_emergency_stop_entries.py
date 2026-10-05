"""El archivo `PARAR` bloquea las entradas nuevas (abrir y aprobar) pero no
rechazar, y deja el motivo registrado."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from decimal import Decimal

from app.domain.enums import EstadoCiclo, EstadoOrden, TierEntrada
from app.domain.rules.execution import CandidatoEntrada
from app.domain.rules.risk import MotivoRechazoEntrada
from app.execution.order_manager import (
    ResultadoAprobacion,
    approve_pending_entry,
    build_cupos,
    open_entries,
    reject_pending_entry,
)
from app.repositories.analysis import create_ciclo, save_propuesta
from app.repositories.orders import list_orders
from app.repositories.positions import list_open_posiciones
from tests.integration.test_analysis_repository import _propuesta_row
from tests.integration.test_order_manager import FakePrices

_AHORA = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)


class Parada:
    def __init__(self, activa: bool):
        self.activa = activa

    def is_active(self) -> bool:
        return self.activa


def _candidato(db_session, symbol="BTCUSDT", tier=TierEntrada.NORMAL, puntaje="64.60") -> CandidatoEntrada:
    ciclo = create_ciclo(db_session, started_at=_AHORA, status=EstadoCiclo.PREFILTER)
    row = dataclasses.replace(_propuesta_row(ciclo, symbol), tier_entrada=tier, puntaje_ponderado=Decimal(puntaje))
    return CandidatoEntrada(save_propuesta(db_session, row), symbol, tier, Decimal(puntaje))


def test_los_cupos_reflejan_si_hay_parada(db_session):
    assert build_cupos(db_session, _AHORA, emergency_stop=Parada(False)).parada_emergencia is False
    assert build_cupos(db_session, _AHORA, emergency_stop=Parada(True)).parada_emergencia is True


def test_con_parar_no_se_abre_ninguna_entrada_y_queda_el_motivo(db_session):
    cand = _candidato(db_session)
    res = open_entries(
        db_session, [cand], now=_AHORA, price_source=FakePrices({"BTCUSDT": "100"}), emergency_stop=Parada(True)
    )
    assert res.procesadas == ()
    assert MotivoRechazoEntrada.PARADA_DE_EMERGENCIA in res.descartadas[0].motivos
    assert list_open_posiciones(db_session) == []
    assert list_orders(db_session, estados=list(EstadoOrden)) == []


def test_sin_parar_la_entrada_se_abre_normal(db_session):
    cand = _candidato(db_session)
    res = open_entries(
        db_session, [cand], now=_AHORA, price_source=FakePrices({"BTCUSDT": "100"}), emergency_stop=Parada(False)
    )
    assert len(res.procesadas) == 1 and len(list_open_posiciones(db_session)) == 1


def test_con_el_archivo_real_parar_tambien_se_bloquea_por_defecto(db_session):
    from app.execution import emergency_stop

    emergency_stop.PARAR_PATH.write_text("")
    cand = _candidato(db_session)
    res = open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({"BTCUSDT": "100"}))
    assert MotivoRechazoEntrada.PARADA_DE_EMERGENCIA in res.descartadas[0].motivos


def test_aprobar_con_parar_se_bloquea_pero_rechazar_sigue_permitido(db_session):
    cand = _candidato(db_session, "ETHUSDT", TierEntrada.MAYOR_RIESGO, "52")
    open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices(), emergency_stop=Parada(False))
    (pendiente,) = list_orders(db_session, estados=[EstadoOrden.PENDING_APPROVAL])

    res = approve_pending_entry(
        db_session,
        pendiente.id,
        now=_AHORA,
        price_source=FakePrices({"ETHUSDT": "100"}),
        emergency_stop=Parada(True),
    )
    assert res == ResultadoAprobacion.BLOQUEADA_POR_PARADA
    assert list_open_posiciones(db_session) == []
    assert list_orders(db_session, estados=[EstadoOrden.PENDING_APPROVAL])[0].id == pendiente.id

    assert reject_pending_entry(db_session, pendiente.id, now=_AHORA) is True


def test_al_soltar_el_freno_la_misma_entrada_pendiente_se_puede_aprobar(db_session):
    cand = _candidato(db_session, "ETHUSDT", TierEntrada.MAYOR_RIESGO, "52")
    open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices(), emergency_stop=Parada(False))
    (pendiente,) = list_orders(db_session, estados=[EstadoOrden.PENDING_APPROVAL])
    precios = FakePrices({"ETHUSDT": "100"})

    assert (
        approve_pending_entry(db_session, pendiente.id, now=_AHORA, price_source=precios, emergency_stop=Parada(True))
        == ResultadoAprobacion.BLOQUEADA_POR_PARADA
    )
    assert (
        approve_pending_entry(db_session, pendiente.id, now=_AHORA, price_source=precios, emergency_stop=Parada(False))
        == ResultadoAprobacion.APROBADA
    )
