"""Tests de app.repositories.analysis contra Postgres real (Paso 7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import update

from app.db.models.analysis import Propuesta
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
    ciclo_id = create_ciclo(db_session, started_at=started_at, status=EstadoCiclo.PREFILTER)

    ciclo = get_ciclo(db_session, ciclo_id)
    assert ciclo is not None
    assert ciclo.status == EstadoCiclo.PREFILTER
    assert ciclo.finished_at is None

    avanzar_ciclo(db_session, ciclo_id, EstadoCiclo.EVENT_CORRELATION)
    assert get_ciclo(db_session, ciclo_id).status == EstadoCiclo.EVENT_CORRELATION

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

    primera = save_propuesta(db_session, _propuesta_row(ciclo_id, symbol="BTCUSDT"))
    segunda = save_propuesta(db_session, _propuesta_row(ciclo_id, symbol="SOLUSDT"))

    # `created_at` usa `now()` de Postgres, que es la hora de INICIO de la
    # transacción: aquí ambas filas comparten transacción y empatarían, con
    # un orden indefinido (el test era intermitente). En producción cada
    # propuesta se confirma en su propia transacción y no hay empate. Se
    # fijan tiempos distintos para probar lo que de verdad importa: que se
    # listan por `created_at` ascendente.
    ahora = datetime.now(timezone.utc)
    db_session.execute(update(Propuesta).where(Propuesta.id == primera).values(created_at=ahora - timedelta(minutes=2)))
    db_session.execute(update(Propuesta).where(Propuesta.id == segunda).values(created_at=ahora - timedelta(minutes=1)))
    db_session.flush()

    propuestas = get_propuestas_de_ciclo(db_session, ciclo_id)
    assert [p.symbol for p in propuestas] == ["BTCUSDT", "SOLUSDT"]


# --- get_puntajes_de_ciclos_previos (observación de racha, 2026-10-04) ------------

from app.repositories.analysis import get_puntajes_de_ciclos_previos  # noqa: E402

_BASE = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)


def _ciclo_cerrado(session, *, dias_atras: int, puntaje: str | None, symbol: str = "LTCUSDT", error: str | None = None,
                   terminado: bool = True, horas_extra: int = 0):
    ciclo_id = create_ciclo(
        session, started_at=_BASE - timedelta(days=dias_atras) + timedelta(hours=horas_extra), status=EstadoCiclo.PREFILTER
    )
    if terminado:
        finalizar_ciclo(
            session, ciclo_id, status=EstadoCiclo.CONSOLIDATE, finished_at=_BASE, cost_usd=Decimal("0.5"),
            error_message=error,
        )
    if puntaje is not None:
        fila = _propuesta_row(ciclo_id, symbol=symbol, supera=False)
        from dataclasses import replace

        save_propuesta(session, replace(fila, puntaje_ponderado=Decimal(puntaje)))
    return ciclo_id


def test_previos_del_mas_reciente_al_mas_antiguo_con_none_si_no_hubo_propuesta(db_session):
    _ciclo_cerrado(db_session, dias_atras=3, puntaje="51.00")
    _ciclo_cerrado(db_session, dias_atras=2, puntaje=None)  # ciclo exitoso sin propuesta de LTC
    _ciclo_cerrado(db_session, dias_atras=1, puntaje="53.00")
    actual = create_ciclo(db_session, started_at=_BASE, status=EstadoCiclo.PREFILTER)

    assert get_puntajes_de_ciclos_previos(db_session, "LTCUSDT", actual) == [Decimal("53.00"), None, Decimal("51.00")]


def test_ignora_ciclos_fallidos_y_no_terminados(db_session):
    _ciclo_cerrado(db_session, dias_atras=3, puntaje="51.00")
    _ciclo_cerrado(db_session, dias_atras=2, puntaje=None, error="falló un agente")
    _ciclo_cerrado(db_session, dias_atras=1, puntaje="52.00", terminado=False)
    actual = create_ciclo(db_session, started_at=_BASE, status=EstadoCiclo.PREFILTER)

    # solo el del día 3: el fallido y el no terminado no produjeron información
    assert get_puntajes_de_ciclos_previos(db_session, "LTCUSDT", actual) == [Decimal("51.00")]


def test_otro_ciclo_del_mismo_dia_del_actual_no_cuenta_como_dia_anterior(db_session):
    _ciclo_cerrado(db_session, dias_atras=1, puntaje="52.00")
    _ciclo_cerrado(db_session, dias_atras=0, puntaje="53.00", horas_extra=-3)  # misma fecha UTC, 3h antes
    actual = create_ciclo(db_session, started_at=_BASE, status=EstadoCiclo.PREFILTER)

    assert get_puntajes_de_ciclos_previos(db_session, "LTCUSDT", actual) == [Decimal("52.00")]


def test_varios_ciclos_exitosos_el_mismo_dia_previo_toman_el_mayor_puntaje(db_session):
    _ciclo_cerrado(db_session, dias_atras=1, puntaje="50.50", horas_extra=-2)
    _ciclo_cerrado(db_session, dias_atras=1, puntaje="53.00")
    actual = create_ciclo(db_session, started_at=_BASE, status=EstadoCiclo.PREFILTER)

    assert get_puntajes_de_ciclos_previos(db_session, "LTCUSDT", actual) == [Decimal("53.00")]


def test_solo_considera_propuestas_del_simbolo_pedido(db_session):
    _ciclo_cerrado(db_session, dias_atras=1, puntaje="52.00", symbol="ETHUSDT")
    actual = create_ciclo(db_session, started_at=_BASE, status=EstadoCiclo.PREFILTER)

    assert get_puntajes_de_ciclos_previos(db_session, "LTCUSDT", actual) == [None]


def test_ciclo_inexistente_lanza_value_error(db_session):
    import uuid

    with pytest.raises(ValueError):
        get_puntajes_de_ciclos_previos(db_session, "LTCUSDT", uuid.uuid4())
