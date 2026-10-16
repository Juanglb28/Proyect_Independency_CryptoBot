"""Tests de `scripts.entries.run_command` (la lógica de los comandos de
terminal) contra Postgres real, con precios falsos."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from decimal import Decimal

from app.domain.enums import EstadoCiclo, TierEntrada
from app.repositories.analysis import create_ciclo, save_propuesta
from scripts.entries import parse_args, run_command
from tests.integration.test_analysis_repository import _propuesta_row
from tests.integration.test_order_manager import FakePrices

_AHORA = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _ciclo_con(db_session, *specs):
    ciclo = create_ciclo(db_session, started_at=_AHORA, status=EstadoCiclo.PREFILTER)
    for symbol, tier, puntaje in specs:
        save_propuesta(
            db_session,
            dataclasses.replace(
                _propuesta_row(ciclo, symbol), tier_entrada=tier, puntaje_ponderado=Decimal(puntaje)
            ),
        )
    return ciclo


def _correr(db_session, argv, precios=None):
    return run_command(
        db_session, parse_args(argv), price_source=FakePrices(precios or {}), now=_AHORA
    )


def test_open_list_approve_y_reject_de_punta_a_punta(db_session):
    ciclo = _ciclo_con(
        db_session,
        ("BTCUSDT", TierEntrada.NORMAL, "70"),
        ("ETHUSDT", TierEntrada.MAYOR_RIESGO, "52"),
        ("SOLUSDT", TierEntrada.MAYOR_RIESGO, "51"),  # segunda de mayor riesgo: tope de 1
    )

    salida = "\n".join(_correr(db_session, ["open", "--ciclo-id", str(ciclo)], {"BTCUSDT": "100"}))
    assert "BTCUSDT" in salida and "ABIERTA" in salida
    assert "ETHUSDT" in salida and "PENDIENTE_APROBACION" in salida
    assert "SOLUSDT" in salida and "DESCARTADA" in salida and "MAXIMO_MAYOR_RIESGO" in salida

    listado = _correr(db_session, ["list"])
    assert listado[0] == "Entradas pendientes de aprobación: 1"
    orden_id = listado[1].split()[0]
    assert any("Posiciones abiertas: 1" in linea for linea in listado)

    aprobada = _correr(db_session, ["approve", orden_id], {"ETHUSDT": "200"})
    assert aprobada[0].startswith("APROBADA")
    assert any("Posiciones abiertas: 2" in linea for linea in _correr(db_session, ["list"]))

    assert _correr(db_session, ["approve", orden_id], {"ETHUSDT": "200"})[0].startswith("NO_PENDIENTE")


def test_reject_y_expire(db_session):
    ciclo = _ciclo_con(db_session, ("ETHUSDT", TierEntrada.MAYOR_RIESGO, "52"))
    _correr(db_session, ["open", "--ciclo-id", str(ciclo)])
    orden_id = _correr(db_session, ["list"])[1].split()[0]

    assert "rechazada" in _correr(db_session, ["reject", orden_id, "--motivo", "no"])[0]
    assert "ya no estaba pendiente" in _correr(db_session, ["reject", orden_id])[0]
    assert _correr(db_session, ["expire"]) == ["Entradas expiradas: 0"]


def test_open_de_un_ciclo_sin_candidatos(db_session):
    ciclo = _ciclo_con(db_session, ("BTCUSDT", TierEntrada.NINGUNO, "40"))
    assert "no tiene propuestas" in _correr(db_session, ["open", "--ciclo-id", str(ciclo)])[0]


def test_open_con_una_discrepancia_no_abre_nada_y_la_muestra(db_session):
    from sqlalchemy import text

    from app.repositories.orders import list_orders
    from app.repositories.positions import list_open_posiciones
    from app.domain.enums import EstadoOrden

    ciclo = _ciclo_con(db_session, ("BTCUSDT", TierEntrada.NORMAL, "70"), ("SOLUSDT", TierEntrada.NORMAL, "66"))
    _correr(db_session, ["open", "--ciclo-id", str(ciclo)], {"BTCUSDT": "100", "SOLUSDT": "50"})
    pos = list_open_posiciones(db_session)[0]
    db_session.execute(text("UPDATE posiciones SET remaining_quantity = 0.5 WHERE id = :id"), {"id": pos.id})
    db_session.expire_all()
    antes = len(list_orders(db_session, estados=[EstadoOrden.FILLED]))

    otro = _ciclo_con(db_session, ("ETHUSDT", TierEntrada.NORMAL, "68"))
    salida = "\n".join(_correr(db_session, ["open", "--ciclo-id", str(otro)], {"ETHUSDT": "2000"}))

    assert "BLOQUEADO" in salida and "CANTIDAD_RESTANTE_NO_COINCIDE" in salida
    assert "ABIERTA" not in salida
    assert len(list_orders(db_session, estados=[EstadoOrden.FILLED])) == antes
