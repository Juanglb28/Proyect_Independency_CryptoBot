"""Corrección auditable de trade_results contra Postgres real (§15.3):
versionado por encadenamiento, trigger de misma posición, índices parciales,
lectura por versión vigente y el servicio `corregir_trade_result`."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.repositories.trade_results import (
    get_trade_result_by_position,
    insertar_correccion,
    list_trade_results,
    list_versions_for_position,
)
from app.results.trade_result_service import (
    EstadoCorreccion,
    corregir_trade_result,
)
from tests.integration.test_results_schema import _cerrar, _valores_de_resultado

D = Decimal
CAPITAL = D("1000")


def _vigente_net_pnl(db_session, position_id) -> Decimal:
    return get_trade_result_by_position(db_session, position_id).net_pnl


def _romper_resultado_guardado(db_session, position_id, net_pnl="999") -> None:
    """Simula un error de cálculo histórico: fuerza un net_pnl equivocado en la
    fila vigente (desactivando el trigger de solo-agregar, como otros tests)."""
    db_session.execute(text("ALTER TABLE trade_results DISABLE TRIGGER trg_trade_results_solo_agregar"))
    db_session.execute(
        text("UPDATE trade_results SET net_pnl = :v WHERE position_id = :p"),
        {"v": D(net_pnl), "p": position_id},
    )
    db_session.execute(text("ALTER TABLE trade_results ENABLE TRIGGER trg_trade_results_solo_agregar"))


# --- servicio corregir_trade_result ---


def test_sin_resultado_no_hay_nada_que_corregir(db_session):
    from app.execution.order_manager import open_entries  # noqa: F401 - asegura imports del pipeline
    r = corregir_trade_result(db_session, uuid.uuid4(), motivo="x", initial_capital=CAPITAL)
    assert r.estado == EstadoCorreccion.SIN_RESULTADO


def test_recalculo_igual_no_crea_version(db_session):
    pos = _cerrar(db_session)
    r = corregir_trade_result(db_session, pos.id, motivo="revisión", initial_capital=CAPITAL)
    assert r.estado == EstadoCorreccion.SIN_CAMBIOS
    assert len(list_versions_for_position(db_session, pos.id)) == 1


def test_correccion_registra_version_nueva_sin_tocar_la_original(db_session):
    pos = _cerrar(db_session)
    correcto = _vigente_net_pnl(db_session, pos.id)
    original_id = get_trade_result_by_position(db_session, pos.id).id
    _romper_resultado_guardado(db_session, pos.id, "999")

    r = corregir_trade_result(
        db_session, pos.id, motivo="error de cálculo en net_pnl", autor="juan", initial_capital=CAPITAL
    )
    assert r.estado == EstadoCorreccion.CORREGIDO
    assert "net_pnl" in r.campos_cambiados
    assert r.version_anterior_id == original_id

    # La original queda intacta (con su valor equivocado) en el historial.
    versiones = list_versions_for_position(db_session, pos.id)
    assert len(versiones) == 2
    original = next(v for v in versiones if v.id == original_id)
    assert original.net_pnl == D("999") and original.supersedes_id is None

    # La vigente es la nueva, con el valor recalculado y el motivo.
    vigente = get_trade_result_by_position(db_session, pos.id)
    assert vigente.id == r.version_nueva_id
    assert vigente.net_pnl == correcto
    assert vigente.supersedes_id == original_id
    assert vigente.correction_reason == "error de cálculo en net_pnl" and vigente.corrected_by == "juan"


def test_dry_run_no_escribe_nada(db_session):
    pos = _cerrar(db_session)
    _romper_resultado_guardado(db_session, pos.id, "999")
    r = corregir_trade_result(db_session, pos.id, motivo="x", initial_capital=CAPITAL, dry_run=True)
    assert r.estado == EstadoCorreccion.CORREGIDO and r.version_nueva_id is None
    assert len(list_versions_for_position(db_session, pos.id)) == 1  # nada nuevo


# --- lectura por versión vigente ---


def test_las_metricas_solo_ven_la_version_vigente(db_session):
    pos = _cerrar(db_session)
    _romper_resultado_guardado(db_session, pos.id, "999")
    corregir_trade_result(db_session, pos.id, motivo="fix", initial_capital=CAPITAL)

    vigentes = list_trade_results(db_session)  # solo_vigentes por defecto
    assert [t.position_id for t in vigentes] == [pos.id]  # una sola fila, no dos
    assert list_trade_results(db_session)[0].net_pnl != D("999")
    # La auditoría sí ve ambas.
    assert len(list_trade_results(db_session, solo_vigentes=False)) == 2


# --- garantías de la base de datos ---


def test_todas_las_versiones_son_solo_agregar(db_session):
    pos = _cerrar(db_session)
    _romper_resultado_guardado(db_session, pos.id, "999")
    corregir_trade_result(db_session, pos.id, motivo="fix", initial_capital=CAPITAL)
    for sentencia in ("UPDATE trade_results SET net_pnl = 0", "DELETE FROM trade_results"):
        with pytest.raises(DBAPIError, match="solo-agregar"):
            with db_session.begin_nested():
                db_session.execute(text(sentencia))


def test_corregir_la_misma_version_es_idempotente(db_session):
    # `insertar_correccion` es idempotente por `supersedes_id` (retry-safe): una
    # segunda llamada devuelve la corrección existente, no crea otra ni lanza.
    pos = _cerrar(db_session)
    vigente_id = get_trade_result_by_position(db_session, pos.id).id
    valores = _valores_de_resultado(db_session, pos)
    id1, creada1 = insertar_correccion(db_session, supersedes_id=vigente_id, valores={**valores, "net_pnl": D("1")}, motivo="a")
    id2, creada2 = insertar_correccion(db_session, supersedes_id=vigente_id, valores={**valores, "net_pnl": D("2")}, motivo="b")
    assert creada1 is True and creada2 is False and id1 == id2
    assert len(list_versions_for_position(db_session, pos.id)) == 2  # original + una sola corrección


def test_el_indice_prohibe_bifurcar_la_cadena(db_session):
    # Dos correcciones distintas de la MISMA versión (sin pasar por el ON CONFLICT
    # de insertar_correccion): el índice único parcial lo rechaza.
    pos = _cerrar(db_session)
    vigente_id = get_trade_result_by_position(db_session, pos.id).id
    valores = _valores_de_resultado(db_session, pos)
    insertar_correccion(db_session, supersedes_id=vigente_id, valores={**valores, "net_pnl": D("1")}, motivo="a")
    columnas = ", ".join(valores)
    marcadores = ", ".join(f":{k}" for k in valores)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(
                text(
                    f"INSERT INTO trade_results (id, supersedes_id, correction_reason, {columnas}) "
                    f"VALUES (:id, :sup, 'otra', {marcadores})"
                ),
                {"id": uuid.uuid4(), "sup": vigente_id, **valores},
            )


def test_una_correccion_debe_ser_de_la_misma_posicion(db_session):
    pos = _cerrar(db_session)
    otra = _cerrar(db_session, symbol="ETHUSDT")
    otra_result_id = get_trade_result_by_position(db_session, otra.id).id
    valores = _valores_de_resultado(db_session, pos)
    valores = {k: v for k, v in valores.items() if k not in ("supersedes_id", "correction_reason", "corrected_by")}
    # La fila es de `pos` pero dice corregir el resultado de `otra`: el trigger lo rechaza.
    with pytest.raises(DBAPIError, match="misma posición"):
        with db_session.begin_nested():
            insertar_correccion(db_session, supersedes_id=otra_result_id, valores=valores, motivo="cruzada")


def test_check_motivo_obligatorio_a_nivel_de_base(db_session):
    pos = _cerrar(db_session)
    vigente_id = get_trade_result_by_position(db_session, pos.id).id
    valores = _valores_de_resultado(db_session, pos)
    valores = {k: v for k, v in valores.items() if k not in ("supersedes_id", "correction_reason", "corrected_by")}
    # supersedes_id puesto pero sin motivo: lo rechaza el CHECK (saltándonos la guarda de la función).
    db_session.execute(text("ALTER TABLE trade_results DISABLE TRIGGER trg_trade_results_solo_agregar"))
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(
                text(
                    "INSERT INTO trade_results (id, position_id, supersedes_id, symbol, side, entry_time, exit_time, "
                    "duration_seconds, entry_quantity, exit_quantity, entry_price, exit_price, capital_at_entry, "
                    "position_value_at_entry, gross_pnl, gross_return_pct, net_pnl, net_return_pct, thesis_status, "
                    "exit_reason) VALUES (:id, :pos, :sup, 'BTCUSDT', 'LONG', now(), now(), 0, 1, 1, 100, 100, 1000, "
                    "100, 0, 0, 0, 0, 'INCONCLUSIVE', 'MANUAL')"
                ),
                {"id": uuid.uuid4(), "pos": pos.id, "sup": vigente_id},
            )
