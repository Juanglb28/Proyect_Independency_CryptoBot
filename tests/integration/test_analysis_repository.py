"""Tests de app.repositories.analysis contra Postgres real (Paso 7)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.domain.enums import EstadoCiclo
from app.repositories.analysis import (
    FactorRow,
    PropuestaRow,
    avanzar_ciclo,
    create_ciclo,
    finalizar_ciclo,
    get_ciclo,
    get_factores_de_propuesta,
    get_propuestas_de_ciclo,
    save_propuesta,
)


def _factores(symbol: str) -> tuple[FactorRow, ...]:
    return (
        FactorRow(symbol=symbol, factor="tendencia_momentum", peso=Decimal("0.30"), puntaje=Decimal(70), confianza="alta"),
        FactorRow(symbol=symbol, factor="volumen_liquidez", peso=Decimal("0.20"), puntaje=Decimal(60), confianza="alta"),
        FactorRow(symbol=symbol, factor="posicion_rangos_ath", peso=Decimal("0.20"), puntaje=Decimal(50), confianza="alta"),
        FactorRow(symbol=symbol, factor="contexto_eventos", peso=Decimal("0.30"), puntaje=Decimal(80), confianza="alta"),
    )


def _propuesta_row(ciclo_id, symbol: str = "BTCUSDT", supera: bool = True) -> PropuestaRow:
    return PropuestaRow(
        ciclo_id=ciclo_id,
        symbol=symbol,
        categoria="principal",
        horizonte="corto",
        nivel_riesgo="medio",
        puntaje_base=Decimal("68.00"),
        calidad_datos=Decimal("95.00"),
        puntaje_ponderado=Decimal("64.60"),
        supera_umbral_entrada=supera,
        tesis="Tesis de prueba.",
        invalidacion="Invalidación de prueba.",
        escenario_adverso={"descripcion": "x", "desencadenante": "y", "probabilidad_relativa": "baja"},
        comentario_chartista=None,
        fuentes=["ripple-sec-case-ends"],
        confianza_global="alta",
        decision_snapshot={"symbol": symbol, "captured_at": "2026-09-30T00:00:00Z"},
        factores=_factores(symbol),
    )


def test_create_avanzar_y_finalizar_ciclo(db_session):
    started_at = datetime.now(timezone.utc)
    ciclo_id = create_ciclo(db_session, started_at=started_at, status=EstadoCiclo.INGEST)

    ciclo = get_ciclo(db_session, ciclo_id)
    assert ciclo is not None
    assert ciclo.status == EstadoCiclo.INGEST
    assert ciclo.finished_at is None

    avanzar_ciclo(db_session, ciclo_id, EstadoCiclo.PREFILTER)
    assert get_ciclo(db_session, ciclo_id).status == EstadoCiclo.PREFILTER

    finalizar_ciclo(
        db_session,
        ciclo_id,
        status=EstadoCiclo.CONSOLIDATE,
        finished_at=datetime.now(timezone.utc),
        cost_usd=Decimal("0.108"),
        error_message=None,
    )
    ciclo = get_ciclo(db_session, ciclo_id)
    assert ciclo.status == EstadoCiclo.CONSOLIDATE
    assert ciclo.finished_at is not None
    assert ciclo.cost_usd == Decimal("0.108")
    assert ciclo.error_message is None


def test_avanzar_ciclo_inexistente_lanza_error(db_session):
    import uuid

    with pytest.raises(ValueError, match="no existe"):
        avanzar_ciclo(db_session, uuid.uuid4(), EstadoCiclo.PREFILTER)


def test_save_propuesta_persiste_propuesta_y_sus_cuatro_factores(db_session):
    ciclo_id = create_ciclo(db_session, started_at=datetime.now(timezone.utc), status=EstadoCiclo.CONSOLIDATE)

    propuesta_id = save_propuesta(db_session, _propuesta_row(ciclo_id))

    propuestas = get_propuestas_de_ciclo(db_session, ciclo_id)
    assert len(propuestas) == 1
    assert propuestas[0].id == propuesta_id
    assert propuestas[0].symbol == "BTCUSDT"
    assert propuestas[0].decision_snapshot["symbol"] == "BTCUSDT"

    factores = get_factores_de_propuesta(db_session, propuesta_id)
    assert len(factores) == 4
    assert {f.factor for f in factores} == {
        "tendencia_momentum",
        "volumen_liquidez",
        "posicion_rangos_ath",
        "contexto_eventos",
    }
    assert all(f.propuesta_id == propuesta_id for f in factores)
    assert all(f.ciclo_id == ciclo_id for f in factores)


def test_todas_las_opciones_se_persisten_aunque_no_superen_el_umbral(db_session):
    """Decisión del usuario (2026-09-30): trazabilidad completa — se
    guardan TODAS las opciones del Consolidador, no solo las que superan
    SCORE_MINIMO_ENTRADA."""
    ciclo_id = create_ciclo(db_session, started_at=datetime.now(timezone.utc), status=EstadoCiclo.CONSOLIDATE)

    save_propuesta(db_session, _propuesta_row(ciclo_id, symbol="ETHUSDT", supera=False))

    propuestas = get_propuestas_de_ciclo(db_session, ciclo_id)
    assert len(propuestas) == 1
    assert propuestas[0].supera_umbral_entrada is False


def test_multiples_propuestas_del_mismo_ciclo_se_listan_en_orden(db_session):
    ciclo_id = create_ciclo(db_session, started_at=datetime.now(timezone.utc), status=EstadoCiclo.CONSOLIDATE)

    save_propuesta(db_session, _propuesta_row(ciclo_id, symbol="BTCUSDT"))
    save_propuesta(db_session, _propuesta_row(ciclo_id, symbol="SOLUSDT"))

    propuestas = get_propuestas_de_ciclo(db_session, ciclo_id)
    assert [p.symbol for p in propuestas] == ["BTCUSDT", "SOLUSDT"]
