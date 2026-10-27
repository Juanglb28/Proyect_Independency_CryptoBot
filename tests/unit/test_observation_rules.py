"""Operaciones de observación (decisión del 2026-10-12): propuesta determinista,
cupos propios y aislamiento de los cupos de la estrategia."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.domain.enums import EstadoPrefiltro, TierEntrada
from app.domain.rules.execution import CandidatoEntrada, select_entries
from app.domain.rules.observation import (
    CONFIANZA_OBSERVACION,
    DatosPrefiltro,
    build_observation_proposal,
    observation_proposal_id,
)
from app.domain.rules.risk import (
    MAX_ENTRADAS_POR_DIA,
    MAX_OBSERVACIONES_ABIERTAS,
    MAX_OBSERVACIONES_POR_DIA,
    MAX_POSICIONES_ABIERTAS,
    MotivoRechazoEntrada,
    SituacionDeCupos,
    validate_entry,
)

D = Decimal
CICLO = uuid.UUID(int=77)


def _datos(symbol="ETHUSDT", estado=EstadoPrefiltro.CANDIDATO, score="52.123456789012", **kw) -> DatosPrefiltro:
    base = dict(
        symbol=symbol,
        estado=estado,
        ranking=2,
        calculated_at=datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc),
        tendencia_pct=D("4.5"),
        volumen_relativo=D("1.8"),
        distancia_max_90d_pct=D("-12.25"),
        score_tendencia=D("60"),
        score_volumen=D("50"),
        score_distancia=D("40"),
        score_final=None if score is None else D(score),
        formula_version="prefilter-v1",
    )
    base.update(kw)
    return DatosPrefiltro(**base)


def _cupos(**overrides) -> SituacionDeCupos:
    defaults = dict(
        posiciones_abiertas=0, entradas_hoy=0, mayor_riesgo_abiertas=0,
        simbolos_abiertos=frozenset(), sistema_pausado=False,
    )
    defaults.update(overrides)
    return SituacionDeCupos(**defaults)


def _c(symbol, puntaje, tier, n):
    return CandidatoEntrada(proposal_id=uuid.UUID(int=n), symbol=symbol, tier=tier, puntaje_ponderado=D(puntaje))


# --- la propuesta de observación -------------------------------------------


def test_el_id_es_determinista_y_depende_del_ciclo_y_del_simbolo():
    a = observation_proposal_id(CICLO, "ETHUSDT")
    assert a == observation_proposal_id(CICLO, "ETHUSDT")
    assert a != observation_proposal_id(CICLO, "BTCUSDT")
    assert a != observation_proposal_id(uuid.UUID(int=78), "ETHUSDT")


def test_arma_la_propuesta_solo_con_lo_que_sabia_el_prefiltro():
    p = build_observation_proposal(CICLO, _datos())
    assert p.id == observation_proposal_id(CICLO, "ETHUSDT")
    assert p.puntaje == D("52.12")  # 2 decimales para la columna
    snap = p.decision_snapshot
    assert snap["origen"] == "OBSERVACION_DETERMINISTA" and snap["sin_ia"] is True
    assert snap["ciclo_id"] == str(CICLO) and snap["symbol"] == "ETHUSDT"
    # el valor completo queda congelado como texto, sin perder precisión
    assert snap["prefiltro"]["score_final"] == "52.123456789012"
    assert snap["prefiltro"]["formula_version"] == "prefilter-v1"
    assert snap["prefiltro"]["vela_diaria_base"] == "2026-10-12T00:00:00+00:00"
    assert "no hay análisis de ia" in p.tesis.lower()
    assert CONFIANZA_OBSERVACION == "no_aplica"


@pytest.mark.parametrize("estado", [EstadoPrefiltro.DESCARTADO_POR_PUNTAJE, EstadoPrefiltro.DESCARTADO_POR_TOPE, EstadoPrefiltro.SIN_DATOS])
def test_solo_un_candidato_puede_observarse(estado):
    with pytest.raises(ValueError, match="CANDIDATO"):
        build_observation_proposal(CICLO, _datos(estado=estado))


def test_un_candidato_sin_puntaje_no_se_inventa():
    with pytest.raises(ValueError, match="sin puntaje"):
        build_observation_proposal(CICLO, _datos(score=None))


# --- validate_entry: cupos propios -------------------------------------------


def test_la_observacion_se_permite_aunque_los_cupos_de_la_estrategia_esten_llenos():
    cupos = _cupos(posiciones_abiertas=MAX_POSICIONES_ABIERTAS, entradas_hoy=MAX_ENTRADAS_POR_DIA)
    v = validate_entry(symbol="ETHUSDT", tier=TierEntrada.OBSERVACION, cupos=cupos)
    assert v.permitida and not v.requiere_aprobacion_manual and v.monto_usd == D(100)


def test_la_estrategia_se_permite_aunque_los_cupos_de_observacion_esten_llenos():
    cupos = _cupos(observacion_abiertas=MAX_OBSERVACIONES_ABIERTAS, observacion_entradas_hoy=MAX_OBSERVACIONES_POR_DIA)
    assert validate_entry(symbol="ETHUSDT", tier=TierEntrada.NORMAL, cupos=cupos).permitida
    assert validate_entry(symbol="ETHUSDT", tier=TierEntrada.MAYOR_RIESGO, cupos=cupos).permitida


def test_limite_exacto_de_observaciones_abiertas():
    ok = validate_entry(
        symbol="ETHUSDT", tier=TierEntrada.OBSERVACION, cupos=_cupos(observacion_abiertas=MAX_OBSERVACIONES_ABIERTAS - 1)
    )
    assert ok.permitida
    lleno = validate_entry(
        symbol="ETHUSDT", tier=TierEntrada.OBSERVACION, cupos=_cupos(observacion_abiertas=MAX_OBSERVACIONES_ABIERTAS)
    )
    assert lleno.motivos_rechazo == (MotivoRechazoEntrada.MAXIMO_OBSERVACIONES,)


def test_limite_exacto_de_observaciones_por_dia():
    ok = validate_entry(
        symbol="ETHUSDT", tier=TierEntrada.OBSERVACION, cupos=_cupos(observacion_entradas_hoy=MAX_OBSERVACIONES_POR_DIA - 1)
    )
    assert ok.permitida
    lleno = validate_entry(
        symbol="ETHUSDT", tier=TierEntrada.OBSERVACION, cupos=_cupos(observacion_entradas_hoy=MAX_OBSERVACIONES_POR_DIA)
    )
    assert lleno.motivos_rechazo == (MotivoRechazoEntrada.MAXIMO_OBSERVACIONES_DIA,)


def test_un_simbolo_abierto_en_cualquier_serie_bloquea_la_observacion():
    v = validate_entry(
        symbol="ETHUSDT", tier=TierEntrada.OBSERVACION, cupos=_cupos(simbolos_abiertos=frozenset({"ETHUSDT"}))
    )
    assert v.motivos_rechazo == (MotivoRechazoEntrada.SIMBOLO_YA_ABIERTO,)


@pytest.mark.parametrize("monto", [D(101), D(200), D(201), D(250), D(99)])
def test_la_observacion_es_siempre_de_100_usd(monto):
    # En particular, nunca puede ejecutarse sin aprobación por encima de 200 USD.
    v = validate_entry(symbol="ETHUSDT", tier=TierEntrada.OBSERVACION, cupos=_cupos(), monto_usd=monto)
    assert not v.permitida and MotivoRechazoEntrada.TAMANO_FUERA_DE_RANGO in v.motivos_rechazo


@pytest.mark.parametrize(
    "ajuste, motivo",
    [
        (dict(sistema_pausado=True), MotivoRechazoEntrada.SISTEMA_PAUSADO),
        (dict(parada_emergencia=True), MotivoRechazoEntrada.PARADA_DE_EMERGENCIA),
        (dict(binance_caido=True), MotivoRechazoEntrada.BINANCE_CAIDO),
    ],
)
def test_pausa_parar_y_binance_caido_tambien_bloquean_la_observacion(ajuste, motivo):
    v = validate_entry(symbol="ETHUSDT", tier=TierEntrada.OBSERVACION, cupos=_cupos(**ajuste))
    assert motivo in v.motivos_rechazo and not v.permitida


def test_simbolo_fuera_de_la_lista_autorizada_nunca_se_observa():
    v = validate_entry(symbol="FOOUSDT", tier=TierEntrada.OBSERVACION, cupos=_cupos())
    assert MotivoRechazoEntrada.SIMBOLO_NO_PERMITIDO in v.motivos_rechazo


# --- select_entries: mezcla de series ------------------------------------------


def test_la_estrategia_entra_antes_que_la_observacion_aunque_tenga_menor_puntaje():
    r = select_entries(
        [_c("ETHUSDT", "95", TierEntrada.OBSERVACION, 1), _c("BTCUSDT", "56", TierEntrada.NORMAL, 2)], _cupos()
    )
    assert [s.candidato.symbol for s in r.seleccionadas] == ["BTCUSDT", "ETHUSDT"]


def test_las_observaciones_consumen_solo_su_propio_cupo():
    from app.domain.symbols import ALLOWED_SYMBOLS

    # Un símbolo más que el tope diario: entran los primeros (desempate alfabético por símbolo) y el último
    # se descarta por el tope diario de observación.
    simbolos = sorted(ALLOWED_SYMBOLS)[: MAX_OBSERVACIONES_POR_DIA + 1]
    candidatos = [_c(s, "50", TierEntrada.OBSERVACION, i) for i, s in enumerate(reversed(simbolos), start=1)]
    r = select_entries(candidatos, _cupos())
    assert [s.candidato.symbol for s in r.seleccionadas] == simbolos[:MAX_OBSERVACIONES_POR_DIA]
    assert [d.candidato.symbol for d in r.descartadas] == [simbolos[MAX_OBSERVACIONES_POR_DIA]]
    assert r.descartadas[0].motivos == (MotivoRechazoEntrada.MAXIMO_OBSERVACIONES_DIA,)


def test_observar_no_gasta_cupos_de_la_estrategia_en_la_misma_seleccion():
    candidatos = [
        _c("BTCUSDT", "50", TierEntrada.OBSERVACION, 1),
        _c("ETHUSDT", "50", TierEntrada.OBSERVACION, 2),
        _c("SOLUSDT", "50", TierEntrada.OBSERVACION, 3),
        _c("ADAUSDT", "60", TierEntrada.NORMAL, 4),
    ]
    r = select_entries(candidatos, _cupos())
    assert {s.candidato.symbol for s in r.seleccionadas} == {"BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT"}
    assert r.descartadas == ()


def test_observacion_y_estrategia_no_pueden_abrir_el_mismo_simbolo():
    r = select_entries(
        [_c("BTCUSDT", "60", TierEntrada.NORMAL, 1), _c("BTCUSDT", "50", TierEntrada.OBSERVACION, 2)], _cupos()
    )
    assert [s.candidato.tier for s in r.seleccionadas] == [TierEntrada.NORMAL]
    assert r.descartadas[0].motivos == (MotivoRechazoEntrada.SIMBOLO_YA_ABIERTO,)
