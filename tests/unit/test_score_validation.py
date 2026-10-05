"""Validación de la capacidad predictiva por bootstrap (sección 8.7)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.rules.results import (
    BOOTSTRAP_ITERACIONES,
    BOOTSTRAP_SEMILLA,
    EstadoValidacion,
    split_terciles,
    validate_score_discrimination,
)
from tests.unit.test_performance import _trade

D = Decimal


def _muestra(n, *, ruido=False):
    """n trades: el score baja con el índice y el P&L también (relación fuerte)."""
    return [
        _trade(str(20 - i * 40 / max(n - 1, 1) + (3 if ruido and i % 2 else 0)), score=str(90 - i), dia=i)
        for i in range(n)
    ]


def test_terciles_con_n_no_multiplo_de_3_excluye_el_resto():
    t = split_terciles(_muestra(7))
    assert (len(t.superior), len(t.medio), len(t.inferior)) == (2, 3, 2)
    scores = [x.initial_opportunity_score for x in t.superior]
    assert scores == sorted(scores, reverse=True) and min(scores) > max(x.initial_opportunity_score for x in t.medio)


def test_terciles_desempate_estable_por_fecha_y_posicion():
    a, b, c = (_trade("1", score="70", dia=i) for i in (2, 0, 1))
    assert [x.entry_time for x in split_terciles([a, b, c, _trade("1", score="10", dia=5)]).superior] == [b.entry_time]
    # el orden de entrada no importa
    assert split_terciles([a, b, c]) == split_terciles([c, b, a])


def test_terciles_ignora_operaciones_sin_score_y_exige_tres():
    assert split_terciles([_trade("1", score=None)] * 5) is None
    assert split_terciles([_trade("1"), _trade("2")]) is None


def test_menos_de_tres_operaciones_es_muestra_insuficiente():
    r = validate_score_discrimination([_trade("1"), _trade("2")])
    assert r.status == EstadoValidacion.MUESTRA_INSUFICIENTE
    assert r.discriminates is None and r.score_discrimination is None and r.ci_lower is None


def test_sin_score_cuenta_aparte():
    r = validate_score_discrimination(_muestra(6) + [_trade("1", score=None)])
    assert r.n_trades == 7 and r.n_trades_con_score == 6
    assert any("sin score" in nota for nota in r.notas)


def test_muestra_menor_a_30_es_preliminar_y_calcula():
    r = validate_score_discrimination(_muestra(12))
    assert r.status == EstadoValidacion.PRELIMINAR
    assert r.tercil_size == 4 and r.score_discrimination > 0
    assert any("Preliminar" in nota for nota in r.notas)


def test_relacion_fuerte_con_30_operaciones_discrimina():
    r = validate_score_discrimination(_muestra(30))
    assert r.status == EstadoValidacion.EVALUABLE
    assert r.high_score_expectancy > r.low_score_expectancy
    assert r.ci_lower > 0 and r.discriminates is True
    assert r.ci_lower <= r.score_discrimination <= r.ci_upper


def test_sin_relacion_el_intervalo_cruza_cero_y_no_discrimina():
    # P&L alternado, independiente del score.
    trades = [_trade("10" if i % 2 else "-10", score=str(90 - i), dia=i) for i in range(30)]
    r = validate_score_discrimination(trades)
    assert r.ci_lower < 0 < r.ci_upper and r.discriminates is False


def test_relacion_inversa_no_discrimina():
    trades = [_trade(str(-20 + i * 40 / 29), score=str(90 - i), dia=i) for i in range(30)]
    r = validate_score_discrimination(trades)
    assert r.score_discrimination < 0 and r.discriminates is False


def test_es_reproducible_con_la_semilla_fija_y_no_depende_del_orden():
    trades = _muestra(30, ruido=True)
    a = validate_score_discrimination(trades)
    assert a == validate_score_discrimination(list(reversed(trades)))
    assert a.seed == BOOTSTRAP_SEMILLA and a.iterations == BOOTSTRAP_ITERACIONES
    assert a.confidence_level_pct == 95 and a.min_trades_plan == 30


def test_otra_semilla_puede_dar_otro_intervalo_pero_misma_diferencia():
    trades = _muestra(30, ruido=True)
    a = validate_score_discrimination(trades)
    b = validate_score_discrimination(trades, seed=1)
    assert a.score_discrimination == b.score_discrimination
    assert (a.ci_lower, a.ci_upper) != (b.ci_lower, b.ci_upper)


def test_el_plan_exige_al_menos_1000_iteraciones():
    with pytest.raises(ValueError):
        validate_score_discrimination(_muestra(30), iterations=999)


def test_indices_del_intervalo_son_los_percentiles_2_5_y_97_5(monkeypatch):
    # Con B=1000 los límites son los elementos 25 y 975 (base 1) de las
    # diferencias ordenadas. Se comprueba contra una réplica independiente.
    import random

    trades = _muestra(12, ruido=True)
    r = validate_score_discrimination(trades)
    orden = sorted(trades, key=lambda t: (-t.initial_opportunity_score, t.entry_time, str(t.position_id)))
    pnl = [t.net_pnl for t in orden]
    rng, k, n, difs = random.Random(BOOTSTRAP_SEMILLA), 4, 12, []
    for _ in range(1000):
        idx = sorted(rng.randrange(n) for _ in range(n))
        difs.append(sum(pnl[i] for i in idx[:k]) / k - sum(pnl[i] for i in idx[n - k:]) / k)
    difs.sort()
    assert r.ci_lower == difs[24].quantize(D("1E-8")) and r.ci_upper == difs[974].quantize(D("1E-8"))


def test_nota_cuando_n_no_es_multiplo_de_3():
    r = validate_score_discrimination(_muestra(7))
    assert r.tercil_size == 2 and any("rango medio" in nota for nota in r.notas)
