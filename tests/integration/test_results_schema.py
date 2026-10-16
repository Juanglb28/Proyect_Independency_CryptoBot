"""Garantías de la base de datos de las tablas del Paso 9: solo-agregar,
idempotencia por clave única y restricciones CHECK."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.domain.enums import AlcanceMetricas, EstadoCiclo, EstadoSnapshot, TierEntrada
from app.domain.rules.execution import CandidatoEntrada
from app.execution.order_manager import open_entries
from app.repositories.analysis import create_ciclo, save_propuesta
from app.repositories.performance import get_performance, list_performance, upsert_performance
from app.repositories.positions import get_open_posicion_by_symbol
from app.repositories.snapshots import append_snapshot, get_price_extremes, list_snapshots_for_position
from app.repositories.trade_results import list_closed_positions_without_result, save_trade_result
from tests.integration.test_analysis_repository import _propuesta_row
from tests.integration.test_results_pipeline import FakePrices, _abrir, _tick, _universo

D = Decimal
T0 = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)


def _snapshot_values(position_id, estado=EstadoSnapshot.SCHEDULED, minutos=1, price="101"):
    return dict(
        position_id=position_id, symbol="BTCUSDT", snapshot_time=T0 + timedelta(minutes=minutos),
        market_price=D(price), quantity=D("1"), position_value=D(price), unrealized_pnl=D("1"),
        unrealized_return_pct=D("1"), trailing_active=False, system_state=estado,
    )


# --- trade_snapshots ---


def test_snapshot_es_idempotente_por_posicion_instante_y_estado(db_session):
    pos = _abrir(db_session)
    _, creado = append_snapshot(db_session, _snapshot_values(pos.id))
    assert creado is True
    _, creado = append_snapshot(db_session, _snapshot_values(pos.id, price="999"))  # mismo instante y estado
    assert creado is False
    # Mismo instante con OTRO estado no choca (un evento real junto a uno programado).
    _, creado = append_snapshot(db_session, _snapshot_values(pos.id, estado=EstadoSnapshot.EXIT))
    assert creado is True
    precios = [s.market_price for s in list_snapshots_for_position(db_session, pos.id) if s.system_state == EstadoSnapshot.SCHEDULED]
    assert precios == [D("101")]  # el reintento no pisó el valor original


def test_snapshots_son_solo_agregar(db_session):
    pos = _abrir(db_session)  # trae su snapshot ENTRY
    with pytest.raises(DBAPIError, match="solo-agregar"):
        with db_session.begin_nested():
            db_session.execute(text("UPDATE trade_snapshots SET market_price = 1"))
    with pytest.raises(DBAPIError, match="solo-agregar"):
        with db_session.begin_nested():
            db_session.execute(text("DELETE FROM trade_snapshots"))
    assert len(list_snapshots_for_position(db_session, pos.id)) == 1


def test_snapshot_rechaza_precio_o_cantidad_no_positivos_y_trailing_sin_referencia(db_session):
    pos = _abrir(db_session)
    for cambio in ({"market_price": D("0")}, {"quantity": D("0")}, {"trailing_active": True}):
        with pytest.raises(IntegrityError):
            with db_session.begin_nested():
                append_snapshot(db_session, {**_snapshot_values(pos.id, minutos=7), **cambio})


def test_snapshot_rechaza_columnas_desconocidas(db_session):
    pos = _abrir(db_session)
    with pytest.raises(ValueError):
        append_snapshot(db_session, {**_snapshot_values(pos.id), "inventada": 1})


def test_extremos_de_precio_de_la_trayectoria(db_session):
    pos = _abrir(db_session)  # ENTRY a 100
    append_snapshot(db_session, _snapshot_values(pos.id, minutos=1, price="95"))
    append_snapshot(db_session, _snapshot_values(pos.id, minutos=2, price="108"))
    assert get_price_extremes(db_session, pos.id) == (D("95"), D("108"))
    assert get_price_extremes(db_session, pos.id, until=T0 + timedelta(minutes=1)) == (D("95"), D("100"))
    assert get_price_extremes(db_session, uuid.uuid4()) is None


# --- trade_results ---


def _cerrar(db_session, symbol="BTCUSDT"):
    pos = _abrir(db_session, symbol=symbol)
    _tick(db_session, _universo(**{symbol: "92"}))
    return pos


def _valores_de_resultado(db_session, pos):
    fila = db_session.execute(text("SELECT * FROM trade_results WHERE position_id = :p"), {"p": pos.id}).mappings().one()
    # Las columnas de versionado (corrección auditable) no son valores del
    # resultado: las pone `insertar_correccion`, no un guardado normal.
    fuera = ("id", "created_at", "supersedes_id", "correction_reason", "corrected_by")
    return {k: v for k, v in fila.items() if k not in fuera}


def test_un_solo_resultado_por_posicion_y_el_reintento_no_lo_toca(db_session):
    pos = _cerrar(db_session)
    valores = _valores_de_resultado(db_session, pos)
    id_original = db_session.execute(text("SELECT id FROM trade_results")).scalar_one()
    id_nuevo, creado = save_trade_result(db_session, {**valores, "net_pnl": D("999")})
    assert creado is False and id_nuevo == id_original
    assert db_session.execute(text("SELECT net_pnl FROM trade_results")).scalar_one() == D("-8.19200000")


def test_trade_results_es_solo_agregar(db_session):
    _cerrar(db_session)
    for sentencia in ("UPDATE trade_results SET net_pnl = 0", "DELETE FROM trade_results"):
        with pytest.raises(DBAPIError, match="solo-agregar"):
            with db_session.begin_nested():
                db_session.execute(text(sentencia))


@pytest.mark.parametrize(
    "cambio",
    [
        {"exit_time": T0 - timedelta(days=30)},  # salida antes de la entrada
        {"exit_quantity": D("0.5")},  # no cerrada del todo
        {"entry_quantity": D("0"), "exit_quantity": D("0")},
        {"exit_price": D("0")},
        {"side": "SHORT"},  # v1 es solo largos
        {"total_fees": D("5")},  # no suma entry_fee + exit_fee
        {"duration_seconds": -1},
    ],
)
def test_trade_results_rechaza_datos_incoherentes(db_session, cambio):
    pos = _cerrar(db_session)
    valores = _valores_de_resultado(db_session, pos)
    # Otra posición cerrada para no chocar con la clave única de `position_id`.
    otra = _cerrar(db_session, symbol="ETHUSDT")
    db_session.execute(text("ALTER TABLE trade_results DISABLE TRIGGER trg_trade_results_solo_agregar"))
    db_session.execute(text("DELETE FROM trade_results WHERE position_id = :p"), {"p": otra.id})
    db_session.execute(text("ALTER TABLE trade_results ENABLE TRIGGER trg_trade_results_solo_agregar"))
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            save_trade_result(db_session, {**valores, "position_id": otra.id, "symbol": "ETHUSDT", **cambio})


def test_trade_results_rechaza_exit_reason_de_entrada(db_session):
    pos = _cerrar(db_session)
    otra = _cerrar(db_session, symbol="ETHUSDT")
    valores = _valores_de_resultado(db_session, pos)
    db_session.execute(text("ALTER TABLE trade_results DISABLE TRIGGER trg_trade_results_solo_agregar"))
    db_session.execute(text("DELETE FROM trade_results WHERE position_id = :p"), {"p": otra.id})
    db_session.execute(text("ALTER TABLE trade_results ENABLE TRIGGER trg_trade_results_solo_agregar"))
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            save_trade_result(db_session, {**valores, "position_id": otra.id, "exit_reason": "ENTRY"})


def test_posiciones_cerradas_sin_resultado_se_listan_para_reparar(db_session):
    pos = _cerrar(db_session)
    assert list_closed_positions_without_result(db_session) == []
    db_session.execute(text("ALTER TABLE trade_results DISABLE TRIGGER trg_trade_results_solo_agregar"))
    db_session.execute(text("DELETE FROM trade_results"))
    db_session.execute(text("ALTER TABLE trade_results ENABLE TRIGGER trg_trade_results_solo_agregar"))
    assert [p.id for p in list_closed_positions_without_result(db_session)] == [pos.id]


# --- performance_metrics ---


def _metrica(scope=AlcanceMetricas.GLOBAL, key=None, **extra):
    return {
        "period_start": T0, "period_end": T0 + timedelta(days=1), "scope_type": scope, "scope_key": key,
        "total_trades": 2, "winning_trades": 1, "losing_trades": 1, "breakeven_trades": 0,
        "net_pnl": D("5"), "calculated_at": T0, **extra,
    }


def test_upsert_reemplaza_el_mismo_alcance_y_periodo_tambien_en_global(db_session):
    id1, creada = upsert_performance(db_session, _metrica())
    id2, creada2 = upsert_performance(db_session, _metrica(net_pnl=D("9")))
    assert (creada, creada2) == (True, False) and id1 == id2
    assert len(list_performance(db_session)) == 1
    m = get_performance(db_session, scope_type=AlcanceMetricas.GLOBAL, scope_key=None,
                        period_start=T0, period_end=T0 + timedelta(days=1))
    assert m.net_pnl == D("9")


def test_alcances_distintos_conviven_en_el_mismo_periodo(db_session):
    upsert_performance(db_session, _metrica())
    upsert_performance(db_session, _metrica(AlcanceMetricas.SYMBOL, "BTCUSDT"))
    upsert_performance(db_session, _metrica(AlcanceMetricas.SYMBOL, "ETHUSDT"))
    assert len(list_performance(db_session)) == 3
    assert len(list_performance(db_session, scope_type=AlcanceMetricas.SYMBOL)) == 2


def test_la_base_impide_dos_global_para_el_mismo_periodo(db_session):
    upsert_performance(db_session, _metrica())
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(
                text(
                    "INSERT INTO performance_metrics (period_start, period_end, scope_type, scope_key, total_trades, "
                    "winning_trades, losing_trades, breakeven_trades) VALUES (:s, :e, 'GLOBAL', NULL, 0, 0, 0, 0)"
                ),
                {"s": T0, "e": T0 + timedelta(days=1)},
            )


@pytest.mark.parametrize(
    "cambio",
    [
        {"scope_type": AlcanceMetricas.GLOBAL, "scope_key": "BTCUSDT"},  # GLOBAL no lleva clave
        {"scope_type": AlcanceMetricas.SYMBOL, "scope_key": None},  # SYMBOL exige clave
        {"period_end": T0 - timedelta(days=1)},
        {"winning_trades": 5},  # los conteos no suman el total
    ],
)
def test_performance_rechaza_datos_incoherentes(db_session, cambio):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            upsert_performance(db_session, {**_metrica(), **cambio})


def test_performance_exige_la_clave_completa_y_columnas_conocidas(db_session):
    with pytest.raises(ValueError):
        upsert_performance(db_session, {"net_pnl": D("1")})
    with pytest.raises(ValueError):
        upsert_performance(db_session, {**_metrica(), "inventada": 1})
