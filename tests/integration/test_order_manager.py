"""Tests del servicio de entradas (`app/execution/order_manager.py`) contra
Postgres real, con un `PriceSource` falso (sin red). Cubren cupos con
reserva, apertura automática, aprobación manual, idempotencia, atomicidad y
que el snapshot de la decisión no se toque."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.enums import (
    EstadoCiclo,
    EstadoOrden,
    EstadoPosicion,
    IntencionOrden,
    TierEntrada,
    TipoEventoSistema,
)
from app.domain.rules.execution import CandidatoEntrada
from app.domain.rules.risk import MotivoRechazoEntrada
from app.execution import order_manager as om
from app.execution.order_manager import (
    ResultadoAprobacion,
    ResultadoEntrada,
    approve_pending_entry,
    build_cupos,
    candidates_from_cycle,
    expire_pending_entries,
    open_entries,
    reject_pending_entry,
)
from app.execution.price_source import PriceUnavailable
from app.repositories.analysis import create_ciclo, get_propuesta, save_propuesta
from app.repositories.orders import get_orden, get_orden_by_client_order_id, list_orders
from app.repositories.positions import count_open_posiciones, get_open_posicion_by_symbol, list_open_posiciones
from app.repositories.system_events import EventoSistemaRow, append_evento_sistema
from tests.integration.test_analysis_repository import _propuesta_row

_AHORA = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
_FIN_DEL_DIA = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)


class FakePrices:
    def __init__(self, precios: dict[str, str] | None = None):
        self.precios = {k: Decimal(v) for k, v in (precios or {}).items()}

    def get_price(self, symbol: str) -> Decimal:
        if symbol not in self.precios:
            raise PriceUnavailable(symbol)
        return self.precios[symbol]


def _ciclo(db_session) -> uuid.UUID:
    return create_ciclo(db_session, started_at=_AHORA, status=EstadoCiclo.PREFILTER)


def _propuesta(db_session, ciclo_id, symbol="BTCUSDT", tier=TierEntrada.NORMAL, puntaje="64.60") -> uuid.UUID:
    row = dataclasses.replace(
        _propuesta_row(ciclo_id, symbol), tier_entrada=tier, puntaje_ponderado=Decimal(puntaje)
    )
    return save_propuesta(db_session, row)


def _candidato(db_session, ciclo_id, symbol="BTCUSDT", tier=TierEntrada.NORMAL, puntaje="64.60") -> CandidatoEntrada:
    pid = _propuesta(db_session, ciclo_id, symbol, tier, puntaje)
    return CandidatoEntrada(pid, symbol, tier, Decimal(puntaje))


# --- candidatos y cupos ---


def test_candidates_from_cycle_ignora_las_que_no_califican(db_session):
    ciclo = _ciclo(db_session)
    normal = _propuesta(db_session, ciclo, "BTCUSDT", TierEntrada.NORMAL)
    riesgo = _propuesta(db_session, ciclo, "ETHUSDT", TierEntrada.MAYOR_RIESGO)
    _propuesta(db_session, ciclo, "SOLUSDT", TierEntrada.NINGUNO)
    _propuesta(db_session, ciclo, "XRPUSDT", None)

    candidatos = candidates_from_cycle(db_session, ciclo)
    assert {c.proposal_id for c in candidatos} == {normal, riesgo}
    assert {c.tier for c in candidatos} == {TierEntrada.NORMAL, TierEntrada.MAYOR_RIESGO}


def test_cupos_vacios(db_session):
    cupos = build_cupos(db_session, _AHORA)
    assert (cupos.posiciones_abiertas, cupos.entradas_hoy, cupos.mayor_riesgo_abiertas) == (0, 0, 0)
    assert cupos.simbolos_abiertos == frozenset()
    assert cupos.sistema_pausado is False


def test_now_sin_zona_horaria_se_rechaza(db_session):
    with pytest.raises(ValueError):
        build_cupos(db_session, datetime(2026, 10, 4, 12, 0))


# --- apertura automática ---


def test_entrada_normal_se_ejecuta_en_paper_con_valores_exactos(db_session):
    ciclo = _ciclo(db_session)
    cand = _candidato(db_session, ciclo)

    res = open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({"BTCUSDT": "100"}))

    assert [p.resultado for p in res.procesadas] == [ResultadoEntrada.ABIERTA]
    pos = get_open_posicion_by_symbol(db_session, "BTCUSDT")
    assert pos.status == EstadoPosicion.OPEN
    assert pos.proposal_id == cand.proposal_id
    assert pos.entry_price == Decimal("100")
    assert pos.initial_quantity == pos.remaining_quantity == Decimal("1")  # 100 USD / 100
    assert pos.initial_stop_price == Decimal("92")  # -8%
    assert pos.initial_target_price == Decimal("112")  # +12%
    assert pos.opened_at == _AHORA

    orden = get_orden_by_client_order_id(db_session, f"entry-{cand.proposal_id}")
    assert orden.estado == EstadoOrden.FILLED
    assert orden.intencion == IntencionOrden.ENTRY
    assert orden.position_id == pos.id
    assert orden.requested_usd == Decimal("100")
    assert orden.execution_price == Decimal("100")
    assert orden.fee == Decimal("0.1")  # 0,1% de 100 USD
    assert orden.filled_at == _AHORA


def test_los_niveles_salen_del_precio_de_ejecucion_y_no_del_del_analisis(db_session):
    ciclo = _ciclo(db_session)
    cand = _candidato(db_session, ciclo)  # el snapshot dice cualquier otra cosa

    open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({"BTCUSDT": "200"}))

    pos = get_open_posicion_by_symbol(db_session, "BTCUSDT")
    assert pos.initial_quantity == Decimal("0.5")
    assert (pos.initial_stop_price, pos.initial_target_price) == (Decimal("184"), Decimal("224"))


def test_el_decision_snapshot_no_se_modifica(db_session):
    ciclo = _ciclo(db_session)
    cand = _candidato(db_session, ciclo)
    antes = dict(get_propuesta(db_session, cand.proposal_id).decision_snapshot)

    open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({"BTCUSDT": "100"}))

    db_session.expire_all()
    assert get_propuesta(db_session, cand.proposal_id).decision_snapshot == antes


def test_sin_precio_no_se_escribe_nada(db_session):
    ciclo = _ciclo(db_session)
    cand = _candidato(db_session, ciclo)

    res = open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({}))

    assert [p.resultado for p in res.procesadas] == [ResultadoEntrada.SIN_PRECIO]
    assert count_open_posiciones(db_session) == 0
    assert list_orders(db_session, estados=list(EstadoOrden)) == []


def test_limite_de_tres_entradas_por_dia(db_session):
    ciclo = _ciclo(db_session)
    candidatos = [
        _candidato(db_session, ciclo, s, puntaje=p)
        for s, p in [("BTCUSDT", "70"), ("ETHUSDT", "69"), ("SOLUSDT", "68"), ("XRPUSDT", "67")]
    ]
    precios = FakePrices({c.symbol: "100" for c in candidatos})

    res = open_entries(db_session, candidatos, now=_AHORA, price_source=precios)

    assert [p.symbol for p in res.procesadas] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert [d.candidato.symbol for d in res.descartadas] == ["XRPUSDT"]
    assert MotivoRechazoEntrada.MAXIMO_ENTRADAS_DIA in res.descartadas[0].motivos
    assert count_open_posiciones(db_session) == 3


def test_el_limite_diario_cuenta_entre_ciclos_del_mismo_dia(db_session):
    ciclo = _ciclo(db_session)
    primeras = [_candidato(db_session, ciclo, s) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")]
    precios = FakePrices({s: "100" for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")})
    open_entries(db_session, primeras, now=_AHORA, price_source=precios)

    mas_tarde = _candidato(db_session, _ciclo(db_session), "XRPUSDT")
    res = open_entries(db_session, [mas_tarde], now=_AHORA + timedelta(hours=3), price_source=precios)

    assert res.procesadas == ()
    assert MotivoRechazoEntrada.MAXIMO_ENTRADAS_DIA in res.descartadas[0].motivos


# --- entradas de mayor riesgo / pendientes ---


def test_mayor_riesgo_queda_pendiente_vence_al_fin_del_dia_y_reserva_cupo(db_session):
    ciclo = _ciclo(db_session)
    cand = _candidato(db_session, ciclo, "ETHUSDT", TierEntrada.MAYOR_RIESGO, "52")

    res = open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({}))

    assert [p.resultado for p in res.procesadas] == [ResultadoEntrada.PENDIENTE_APROBACION]
    orden = get_orden(db_session, res.procesadas[0].orden_id)
    assert orden.estado == EstadoOrden.PENDING_APPROVAL
    assert orden.expires_at == _FIN_DEL_DIA
    assert orden.requested_usd == Decimal("100")
    assert orden.position_id is None and orden.quantity is None
    assert count_open_posiciones(db_session) == 0

    cupos = build_cupos(db_session, _AHORA)
    assert (cupos.posiciones_abiertas, cupos.entradas_hoy, cupos.mayor_riesgo_abiertas) == (1, 1, 1)
    assert cupos.simbolos_abiertos == frozenset({"ETHUSDT"})


def test_un_simbolo_pendiente_no_se_vuelve_a_abrir(db_session):
    ciclo = _ciclo(db_session)
    pendiente = _candidato(db_session, ciclo, "ETHUSDT", TierEntrada.MAYOR_RIESGO, "52")
    open_entries(db_session, [pendiente], now=_AHORA, price_source=FakePrices({}))

    otra = _candidato(db_session, _ciclo(db_session), "ETHUSDT", TierEntrada.NORMAL, "70")
    res = open_entries(db_session, [otra], now=_AHORA, price_source=FakePrices({"ETHUSDT": "100"}))

    assert res.procesadas == ()
    assert MotivoRechazoEntrada.SIMBOLO_YA_ABIERTO in res.descartadas[0].motivos


def test_aprobar_ejecuta_al_precio_del_momento_de_aprobar(db_session):
    ciclo = _ciclo(db_session)
    cand = _candidato(db_session, ciclo, "ETHUSDT", TierEntrada.MAYOR_RIESGO, "52")
    orden_id = open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({})).procesadas[0].orden_id

    ahora = _AHORA + timedelta(hours=2)
    res = approve_pending_entry(db_session, orden_id, now=ahora, price_source=FakePrices({"ETHUSDT": "200"}))

    assert res == ResultadoAprobacion.APROBADA
    pos = get_open_posicion_by_symbol(db_session, "ETHUSDT")
    assert (pos.entry_price, pos.initial_quantity) == (Decimal("200"), Decimal("0.5"))
    assert (pos.initial_stop_price, pos.initial_target_price) == (Decimal("184"), Decimal("224"))
    assert pos.opened_at == ahora
    db_session.refresh(get_orden(db_session, orden_id))
    orden = get_orden(db_session, orden_id)
    assert orden.estado == EstadoOrden.FILLED
    assert orden.position_id == pos.id
    assert (orden.execution_price, orden.fee, orden.resolved_at) == (Decimal("200"), Decimal("0.1"), ahora)


def test_aprobar_dos_veces_no_duplica(db_session):
    ciclo = _ciclo(db_session)
    cand = _candidato(db_session, ciclo, "ETHUSDT", TierEntrada.MAYOR_RIESGO, "52")
    orden_id = open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({})).procesadas[0].orden_id
    precios = FakePrices({"ETHUSDT": "200"})

    assert approve_pending_entry(db_session, orden_id, now=_AHORA, price_source=precios) == ResultadoAprobacion.APROBADA
    assert approve_pending_entry(db_session, orden_id, now=_AHORA, price_source=precios) == ResultadoAprobacion.NO_PENDIENTE
    assert count_open_posiciones(db_session) == 1


def test_aprobar_sin_precio_deja_la_orden_pendiente(db_session):
    ciclo = _ciclo(db_session)
    cand = _candidato(db_session, ciclo, "ETHUSDT", TierEntrada.MAYOR_RIESGO, "52")
    orden_id = open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({})).procesadas[0].orden_id

    res = approve_pending_entry(db_session, orden_id, now=_AHORA, price_source=FakePrices({}))

    assert res == ResultadoAprobacion.SIN_PRECIO
    assert get_orden(db_session, orden_id).estado == EstadoOrden.PENDING_APPROVAL
    assert count_open_posiciones(db_session) == 0


def test_aprobar_despues_del_vencimiento_la_expira_en_vez_de_ejecutarla(db_session):
    ciclo = _ciclo(db_session)
    cand = _candidato(db_session, ciclo, "ETHUSDT", TierEntrada.MAYOR_RIESGO, "52")
    orden_id = open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({})).procesadas[0].orden_id

    res = approve_pending_entry(
        db_session, orden_id, now=_FIN_DEL_DIA, price_source=FakePrices({"ETHUSDT": "200"})
    )

    assert res == ResultadoAprobacion.EXPIRADA
    db_session.refresh(get_orden(db_session, orden_id))
    assert get_orden(db_session, orden_id).estado == EstadoOrden.EXPIRED
    assert count_open_posiciones(db_session) == 0


def test_aprobar_orden_inexistente(db_session):
    with pytest.raises(ValueError):
        approve_pending_entry(db_session, uuid.uuid4(), now=_AHORA, price_source=FakePrices({}))


def test_rechazar_devuelve_todos_los_cupos(db_session):
    ciclo = _ciclo(db_session)
    cand = _candidato(db_session, ciclo, "ETHUSDT", TierEntrada.MAYOR_RIESGO, "52")
    orden_id = open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({})).procesadas[0].orden_id

    assert reject_pending_entry(db_session, orden_id, now=_AHORA, detail="no me convence") is True

    cupos = build_cupos(db_session, _AHORA)
    assert (cupos.posiciones_abiertas, cupos.entradas_hoy, cupos.mayor_riesgo_abiertas) == (0, 0, 0)
    assert cupos.simbolos_abiertos == frozenset()
    assert reject_pending_entry(db_session, orden_id, now=_AHORA) is False  # ya no estaba pendiente


def test_expirar_devuelve_todos_los_cupos(db_session):
    ciclo = _ciclo(db_session)
    cand = _candidato(db_session, ciclo, "ETHUSDT", TierEntrada.MAYOR_RIESGO, "52")
    open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({}))

    assert expire_pending_entries(db_session, _AHORA) == 0  # todavía no vence
    assert expire_pending_entries(db_session, _FIN_DEL_DIA) == 1

    cupos = build_cupos(db_session, _FIN_DEL_DIA)
    assert (cupos.posiciones_abiertas, cupos.entradas_hoy, cupos.mayor_riesgo_abiertas) == (0, 0, 0)


def test_open_entries_libera_primero_las_aprobaciones_vencidas(db_session):
    ciclo = _ciclo(db_session)
    pendiente = _candidato(db_session, ciclo, "ETHUSDT", TierEntrada.MAYOR_RIESGO, "52")
    open_entries(db_session, [pendiente], now=_AHORA, price_source=FakePrices({}))

    al_dia_siguiente = _FIN_DEL_DIA + timedelta(hours=8)
    nueva = _candidato(db_session, _ciclo(db_session), "ETHUSDT", TierEntrada.NORMAL, "70")
    res = open_entries(db_session, [nueva], now=al_dia_siguiente, price_source=FakePrices({"ETHUSDT": "100"}))

    assert res.expiradas == 1
    assert [p.resultado for p in res.procesadas] == [ResultadoEntrada.ABIERTA]


# --- idempotencia ---


def test_una_propuesta_rechazada_no_vuelve_a_intentarse(db_session):
    ciclo = _ciclo(db_session)
    cand = _candidato(db_session, ciclo, "ETHUSDT", TierEntrada.MAYOR_RIESGO, "52")
    orden_id = open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({})).procesadas[0].orden_id
    reject_pending_entry(db_session, orden_id, now=_AHORA)

    res = open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({}))

    assert [p.resultado for p in res.procesadas] == [ResultadoEntrada.YA_PROCESADA]
    assert len(list_orders(db_session, estados=list(EstadoOrden))) == 1
    assert count_open_posiciones(db_session) == 0


def test_reintentar_la_misma_entrada_automatica_no_duplica(db_session):
    ciclo = _ciclo(db_session)
    cand = _candidato(db_session, ciclo)
    precios = FakePrices({"BTCUSDT": "100"})
    open_entries(db_session, [cand], now=_AHORA, price_source=precios)

    res = open_entries(db_session, [cand], now=_AHORA, price_source=precios)  # reintento

    assert res.procesadas == ()  # el símbolo ya está abierto: ni se intenta
    assert MotivoRechazoEntrada.SIMBOLO_YA_ABIERTO in res.descartadas[0].motivos
    assert len(list_orders(db_session, estados=list(EstadoOrden))) == 1
    assert count_open_posiciones(db_session) == 1


# --- pausa por drawdown ---


def _pausar(db_session):
    append_evento_sistema(
        db_session,
        EventoSistemaRow(
            event_type=TipoEventoSistema.PAUSA_DRAWDOWN,
            occurred_at=_AHORA,
            equity_usd=Decimal("850"),
            peak_equity_usd=Decimal("1000"),
        ),
    )


def test_con_el_sistema_pausado_no_se_abre_ninguna_entrada(db_session):
    ciclo = _ciclo(db_session)
    cand = _candidato(db_session, ciclo)
    _pausar(db_session)

    res = open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({"BTCUSDT": "100"}))

    assert res.procesadas == ()
    assert MotivoRechazoEntrada.SISTEMA_PAUSADO in res.descartadas[0].motivos
    assert count_open_posiciones(db_session) == 0


def test_aprobar_con_el_sistema_pausado_se_bloquea_pero_rechazar_sigue_permitido(db_session):
    ciclo = _ciclo(db_session)
    cand = _candidato(db_session, ciclo, "ETHUSDT", TierEntrada.MAYOR_RIESGO, "52")
    orden_id = open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({})).procesadas[0].orden_id
    _pausar(db_session)

    res = approve_pending_entry(db_session, orden_id, now=_AHORA, price_source=FakePrices({"ETHUSDT": "200"}))

    assert res == ResultadoAprobacion.BLOQUEADA_POR_PAUSA
    assert count_open_posiciones(db_session) == 0
    assert reject_pending_entry(db_session, orden_id, now=_AHORA) is True


# --- atomicidad ---


def test_si_falla_la_aprobacion_a_mitad_la_posicion_se_revierte_con_la_orden(db_session, monkeypatch):
    ciclo = _ciclo(db_session)
    cand = _candidato(db_session, ciclo, "ETHUSDT", TierEntrada.MAYOR_RIESGO, "52")
    orden_id = open_entries(db_session, [cand], now=_AHORA, price_source=FakePrices({})).procesadas[0].orden_id
    monkeypatch.setattr(om, "resolve_pending_order", lambda *a, **k: False)  # otra resolución se adelantó

    with pytest.raises(RuntimeError):
        with db_session.begin_nested():  # el `session_scope` real revierte toda la transacción
            approve_pending_entry(db_session, orden_id, now=_AHORA, price_source=FakePrices({"ETHUSDT": "200"}))

    assert count_open_posiciones(db_session) == 0
    assert get_orden(db_session, orden_id).estado == EstadoOrden.PENDING_APPROVAL
