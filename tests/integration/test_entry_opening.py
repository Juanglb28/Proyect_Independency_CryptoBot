"""Apertura automática de entradas al terminar un ciclo
(`app/orchestration/entry_opening.py`, decisión del 2026-10-12) contra Postgres
real, con precios falsos: estrategia + observación, cupos propios, idempotencia,
series separadas y aislamiento del equity."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.domain.enums import (
    EstadoCiclo,
    EstadoOrden,
    EstadoPrefiltro,
    IntencionOrden,
    SerieOperacion,
    TierEntrada,
)
from app.domain.rules.observation import observation_proposal_id
from app.execution.monitor import current_equity
from app.execution.order_manager import build_cupos
from app.orchestration.entry_opening import abrir_entradas_del_ciclo
from app.repositories.analysis import (
    PrefiltroRow,
    create_ciclo,
    get_propuesta,
    get_propuestas_de_ciclo,
    get_puntajes_de_ciclos_previos,
    insertar_propuesta_observacion,
    save_prefiltro_resultados,
    save_propuesta,
)
from app.repositories.orders import count_entry_orders, list_orders
from app.repositories.positions import list_open_posiciones
from tests.integration.test_analysis_repository import _propuesta_row
from tests.integration.test_order_manager import FakePrices

D = Decimal
_AHORA = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
_PRECIOS = {"BTCUSDT": "100", "ETHUSDT": "50", "SOLUSDT": "20", "ADAUSDT": "2", "XRPUSDT": "1"}


def _prefiltro(symbol, estado=EstadoPrefiltro.CANDIDATO, score="52.5", ranking=1) -> PrefiltroRow:
    con_datos = estado != EstadoPrefiltro.SIN_DATOS
    return PrefiltroRow(
        symbol=symbol,
        estado=estado,
        ranking=ranking if con_datos else None,
        calculated_at=_AHORA if con_datos else None,
        tendencia_pct=D("4.5") if con_datos else None,
        volumen_relativo=D("1.8") if con_datos else None,
        distancia_max_90d_pct=D("-12.25") if con_datos else None,
        score_tendencia=D("60") if con_datos else None,
        score_volumen=D("50") if con_datos else None,
        score_distancia=D("40") if con_datos else None,
        score_final=D(score) if con_datos else None,
    )


def _ciclo(db_session, *filas: PrefiltroRow) -> uuid.UUID:
    ciclo = create_ciclo(db_session, started_at=_AHORA, status=EstadoCiclo.PREFILTER)
    save_prefiltro_resultados(db_session, ciclo, list(filas), formula_version="prefilter-v1")
    return ciclo


def _estrategia(db_session, ciclo, symbol, tier=TierEntrada.NORMAL, puntaje="64.60"):
    return save_propuesta(
        db_session,
        dataclasses.replace(_propuesta_row(ciclo, symbol), tier_entrada=tier, puntaje_ponderado=D(puntaje)),
    )


def _abrir(db_session, ciclo, precios=None):
    return abrir_entradas_del_ciclo(
        db_session, ciclo, now=_AHORA, price_source=FakePrices(precios if precios is not None else _PRECIOS)
    )


# --- observación ---------------------------------------------------------------


def test_un_candidato_del_prefiltro_abre_una_posicion_de_observacion(db_session):
    ciclo = _ciclo(db_session, _prefiltro("ETHUSDT", score="52.5"))

    apertura = _abrir(db_session, ciclo)

    assert [(e.symbol, e.serie, e.resultado) for e in apertura.entradas] == [
        ("ETHUSDT", SerieOperacion.OBSERVACION, "ABIERTA")
    ]
    propuesta = get_propuesta(db_session, observation_proposal_id(ciclo, "ETHUSDT"))
    assert propuesta is not None
    assert propuesta.tier_entrada == TierEntrada.OBSERVACION
    assert propuesta.calidad_datos is None  # sin Auditor: no se inventa
    assert propuesta.puntaje_ponderado == D("52.50")
    assert propuesta.decision_snapshot["origen"] == "OBSERVACION_DETERMINISTA"
    assert D(propuesta.decision_snapshot["prefiltro"]["score_final"]) == D("52.5")

    abiertas = list_open_posiciones(db_session)
    assert [p.symbol for p in abiertas] == ["ETHUSDT"]
    assert abiertas[0].proposal_id == propuesta.id
    orden = list_orders(db_session, estados=[EstadoOrden.FILLED], intencion=IntencionOrden.ENTRY)[0]
    assert orden.requested_usd == D(100)  # tamaño fijo, sin aprobación manual


def test_solo_los_candidatos_abren_observaciones(db_session):
    ciclo = _ciclo(
        db_session,
        _prefiltro("ETHUSDT", EstadoPrefiltro.CANDIDATO, "52", ranking=1),
        _prefiltro("SOLUSDT", EstadoPrefiltro.DESCARTADO_POR_PUNTAJE, "30", ranking=2),
        _prefiltro("ADAUSDT", EstadoPrefiltro.DESCARTADO_POR_TOPE, "47", ranking=3),
        _prefiltro("XRPUSDT", EstadoPrefiltro.SIN_DATOS),
    )

    apertura = _abrir(db_session, ciclo)

    assert [e.symbol for e in apertura.entradas] == ["ETHUSDT"]
    assert [p.symbol for p in list_open_posiciones(db_session)] == ["ETHUSDT"]


def test_reintentar_el_mismo_ciclo_no_duplica_propuesta_ni_posicion(db_session):
    ciclo = _ciclo(db_session, _prefiltro("ETHUSDT"))
    _abrir(db_session, ciclo)

    segunda = _abrir(db_session, ciclo)

    assert len(list_open_posiciones(db_session)) == 1
    assert len(get_propuestas_de_ciclo(db_session, ciclo)) == 1
    assert len(list_orders(db_session, estados=list(EstadoOrden), intencion=IntencionOrden.ENTRY)) == 1
    # el símbolo ya está abierto: se informa como descartada y NO deja otra fila
    assert [(e.resultado, e.motivos) for e in segunda.entradas] == [("DESCARTADA", ("SIMBOLO_YA_ABIERTO",))]


def test_la_estrategia_tiene_prioridad_y_la_observacion_del_mismo_simbolo_no_deja_fila(db_session):
    ciclo = _ciclo(db_session, _prefiltro("BTCUSDT", score="60", ranking=1))
    estrategia_id = _estrategia(db_session, ciclo, "BTCUSDT", TierEntrada.NORMAL)

    apertura = _abrir(db_session, ciclo)

    por_serie = {(e.serie, e.resultado) for e in apertura.entradas}
    assert (SerieOperacion.ESTRATEGIA, "ABIERTA") in por_serie
    assert (SerieOperacion.OBSERVACION, "DESCARTADA") in por_serie
    assert [p.id for p in get_propuestas_de_ciclo(db_session, ciclo)] == [estrategia_id]
    assert [p.proposal_id for p in list_open_posiciones(db_session)] == [estrategia_id]


def test_cupo_diario_propio_de_observacion_y_sin_filas_huerfanas(db_session):
    simbolos = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT"]
    ciclo = _ciclo(db_session, *[_prefiltro(s, score="50", ranking=i) for i, s in enumerate(simbolos, 1)])

    apertura = _abrir(db_session, ciclo)

    abiertas = [e for e in apertura.entradas if e.resultado == "ABIERTA"]
    descartadas = [e for e in apertura.entradas if e.resultado == "DESCARTADA"]
    assert len(abiertas) == 3 and len(descartadas) == 1
    assert descartadas[0].motivos == ("MAXIMO_OBSERVACIONES_DIA",)
    # solo se guardó la propuesta de las 3 que abrieron
    assert len(get_propuestas_de_ciclo(db_session, ciclo)) == 3
    assert descartadas[0].symbol not in {p.symbol for p in get_propuestas_de_ciclo(db_session, ciclo)}


def test_las_observaciones_no_gastan_los_cupos_de_la_estrategia(db_session):
    simbolos = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    ciclo = _ciclo(db_session, *[_prefiltro(s, ranking=i) for i, s in enumerate(simbolos, 1)])
    _abrir(db_session, ciclo)

    cupos = build_cupos(db_session, _AHORA)

    assert cupos.observacion_abiertas == 3 and cupos.observacion_entradas_hoy == 3
    assert cupos.posiciones_abiertas == 0 and cupos.entradas_hoy == 0 and cupos.mayor_riesgo_abiertas == 0
    assert cupos.simbolos_abiertos == frozenset(simbolos)

    # y la estrategia sigue pudiendo abrir sus 100 USD
    otro = _ciclo(db_session, _prefiltro("ADAUSDT", score="40", ranking=1))  # no es candidato útil
    _estrategia(db_session, otro, "ADAUSDT", TierEntrada.NORMAL)
    apertura = _abrir(db_session, otro)
    assert (SerieOperacion.ESTRATEGIA, "ABIERTA") in {(e.serie, e.resultado) for e in apertura.entradas}


def test_sin_precio_no_se_abre_ni_se_inventa_uno(db_session):
    ciclo = _ciclo(db_session, _prefiltro("ETHUSDT"))

    apertura = _abrir(db_session, ciclo, precios={})

    assert [e.resultado for e in apertura.entradas] == ["SIN_PRECIO"]
    assert list_open_posiciones(db_session) == []


def test_un_ciclo_sin_candidatos_ni_propuestas_no_hace_nada(db_session):
    ciclo = _ciclo(db_session, _prefiltro("ETHUSDT", EstadoPrefiltro.DESCARTADO_POR_PUNTAJE, "30"))

    apertura = _abrir(db_session, ciclo)

    assert apertura.entradas == ()
    assert list_open_posiciones(db_session) == []


def test_una_discrepancia_de_reconciliacion_bloquea_la_apertura(db_session):
    primero = _ciclo(db_session, _prefiltro("ETHUSDT"))
    _abrir(db_session, primero)
    posicion = list_open_posiciones(db_session)[0]
    db_session.execute(text("UPDATE posiciones SET remaining_quantity = 0.5 WHERE id = :id"), {"id": posicion.id})
    db_session.expire_all()

    segundo = _ciclo(db_session, _prefiltro("SOLUSDT"))
    apertura = _abrir(db_session, segundo)

    assert apertura.bloqueada_por_reconciliacion and apertura.entradas == ()
    assert get_propuestas_de_ciclo(db_session, segundo) == []


# --- series separadas ------------------------------------------------------------


def test_los_filtros_de_serie_separan_ordenes_y_posiciones(db_session):
    ciclo = _ciclo(db_session, _prefiltro("ETHUSDT", ranking=1), _prefiltro("BTCUSDT", score="60", ranking=2))
    _estrategia(db_session, ciclo, "BTCUSDT", TierEntrada.NORMAL)
    _abrir(db_session, ciclo)

    def simbolos(items):
        return sorted(i.symbol for i in items)

    assert simbolos(list_open_posiciones(db_session, serie=SerieOperacion.ESTRATEGIA)) == ["BTCUSDT"]
    assert simbolos(list_open_posiciones(db_session, serie=SerieOperacion.OBSERVACION)) == ["ETHUSDT"]
    assert simbolos(list_open_posiciones(db_session)) == ["BTCUSDT", "ETHUSDT"]
    ordenes_obs = list_orders(
        db_session, estados=list(EstadoOrden), intencion=IntencionOrden.ENTRY, serie=SerieOperacion.OBSERVACION
    )
    assert simbolos(ordenes_obs) == ["ETHUSDT"]
    hoy = dict(
        estados=[EstadoOrden.FILLED],
        created_from=datetime(2026, 10, 12, tzinfo=timezone.utc),
        created_before=datetime(2026, 10, 13, tzinfo=timezone.utc),
    )
    assert count_entry_orders(db_session, serie=SerieOperacion.ESTRATEGIA, **hoy) == 1
    assert count_entry_orders(db_session, serie=SerieOperacion.OBSERVACION, **hoy) == 1


def test_el_equity_ignora_las_posiciones_de_observacion(db_session):
    capital = D(1000)
    ciclo = _ciclo(db_session, _prefiltro("ETHUSDT"))
    _abrir(db_session, ciclo)

    # sin posiciones de estrategia: el equity es el capital, y no pide el precio de ETH
    assert current_equity(db_session, initial_capital=capital, precios={}) == (capital, 0)
    # aunque el precio de la observación caiga a la mitad
    assert current_equity(db_session, initial_capital=capital, precios={"ETHUSDT": D(25)}) == (capital, 0)


# --- restricciones y repositorios -------------------------------------------------


def test_insertar_propuesta_observacion_es_idempotente(db_session):
    ciclo = _ciclo(db_session)
    fila = dataclasses.replace(
        _propuesta_row(ciclo, "ETHUSDT"), calidad_datos=None, tier_entrada=TierEntrada.OBSERVACION, factores=()
    )
    pid = observation_proposal_id(ciclo, "ETHUSDT")

    assert insertar_propuesta_observacion(db_session, fila, propuesta_id=pid) == (pid, True)
    assert insertar_propuesta_observacion(db_session, fila, propuesta_id=pid) == (pid, False)
    assert len(get_propuestas_de_ciclo(db_session, ciclo)) == 1


def test_calidad_de_datos_nula_solo_se_admite_en_observacion(db_session):
    ciclo = _ciclo(db_session)
    sin_calidad = dataclasses.replace(_propuesta_row(ciclo, "BTCUSDT"), calidad_datos=None, tier_entrada=TierEntrada.NORMAL)

    with pytest.raises(IntegrityError, match="calidad_datos_salvo_observacion"):
        with db_session.begin_nested():
            save_propuesta(db_session, sin_calidad)


def test_los_puntajes_previos_ignoran_las_observaciones(db_session):
    ayer = datetime(2026, 10, 11, 0, 5, tzinfo=timezone.utc)
    ciclo_ayer = create_ciclo(db_session, started_at=ayer, status=EstadoCiclo.PREFILTER)
    insertar_propuesta_observacion(
        db_session,
        dataclasses.replace(
            _propuesta_row(ciclo_ayer, "ETHUSDT"), calidad_datos=None, tier_entrada=TierEntrada.OBSERVACION, factores=()
        ),
        propuesta_id=observation_proposal_id(ciclo_ayer, "ETHUSDT"),
    )
    from app.repositories.analysis import finalizar_ciclo

    finalizar_ciclo(
        db_session, ciclo_ayer, status=EstadoCiclo.CONSOLIDATE, finished_at=ayer, cost_usd=None, error_message=None
    )
    hoy = create_ciclo(db_session, started_at=_AHORA, status=EstadoCiclo.PREFILTER)

    # el día de ayer existe, pero su "propuesta" era una observación: cuenta como sin propuesta
    assert get_puntajes_de_ciclos_previos(db_session, "ETHUSDT", hoy) == [None]
