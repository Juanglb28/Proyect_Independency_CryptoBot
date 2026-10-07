"""Análisis por factor (sección 8.8): función pura sobre operaciones cerradas."""

from __future__ import annotations

import dataclasses
import uuid
from decimal import Decimal

import pytest

from app.domain.rules.factors import FACTORES, FactorDeOperacion, analyze_factors
from app.domain.rules.results import (
    BOOTSTRAP_ITERACIONES,
    BOOTSTRAP_SEMILLA,
    EstadoValidacion,
    validate_score_discrimination,
)
from tests.unit.test_performance import _trade

D = Decimal
F0, F1, F2, F3 = FACTORES


def _fo(trade, factor, puntaje, *, peso="0.20", confianza="alta"):
    return FactorDeOperacion(trade.position_id, factor, D(peso), D(str(puntaje)), confianza)


def _muestra(n, *, factor_fuerte=F0):
    """n operaciones: el factor `factor_fuerte` baja junto con el P&L (relación
    fuerte); los demás factores puntúan igual en todas (sin relación)."""
    trades = [_trade(str(20 - i * 40 / max(n - 1, 1)), score="60", dia=i) for i in range(n)]
    factores = []
    for i, t in enumerate(trades):
        for f in FACTORES:
            factores.append(_fo(t, f, 90 - i if f == factor_fuerte else 50, peso="0.30" if f == factor_fuerte else "0.20"))
    return trades, factores


def _por_nombre(rep):
    return {a.factor: a for a in rep.factores}


def test_siempre_devuelve_los_cuatro_factores_en_orden_incluso_sin_datos():
    rep = analyze_factors([], [])
    assert [a.factor for a in rep.factores] == list(FACTORES)
    for a in rep.factores:
        assert a.estado == EstadoValidacion.MUESTRA_INSUFICIENTE
        assert a.n_operaciones == 0 and a.pct_de_operaciones is None
        assert a.contribucion_usd is None and a.discrimina is None and a.tercil_alto is None
        assert a.resultado_promedio_usd is None and a.win_rate_pct is None  # nunca un 0 inventado


def test_menos_de_tres_operaciones_no_forma_terciles_y_lo_dice():
    trades, factores = _muestra(2)
    a = _por_nombre(analyze_factors(trades, factores))[F0]
    assert a.estado == EstadoValidacion.MUESTRA_INSUFICIENTE
    assert a.n_operaciones == 2 and a.contribucion_usd is None
    assert any("Menos de 3" in n for n in a.notas)


def test_frecuencia_cuenta_operaciones_con_el_factor_y_su_porcentaje():
    trades, factores = _muestra(6)
    huerfana = _trade("5", dia=9)  # sin factores guardados
    rep = analyze_factors(trades + [huerfana], factores)
    assert rep.n_operaciones == 7 and rep.n_con_propuesta_y_factores == 6
    a = _por_nombre(rep)[F0]
    assert a.n_operaciones == 6 and a.pct_de_operaciones == D("85.71")
    assert any("1 operación(es) sin propuesta" in n for n in rep.notas)


def test_peso_asignado_es_el_guardado_y_si_cambia_se_dice():
    trades, factores = _muestra(6)
    por = _por_nombre(analyze_factors(trades, factores))
    assert por[F0].peso == D("0.30") and por[F1].peso == D("0.20")

    primera_de_f1 = next(i for i, f in enumerate(factores) if f.factor == F1)
    cambiado = [dataclasses.replace(f, peso=D("0.25")) if i == primera_de_f1 else f for i, f in enumerate(factores)]
    a = _por_nombre(analyze_factors(trades, cambiado))[F1]
    assert a.peso is None and any("peso asignado cambió" in n for n in a.notas)


def test_resultado_promedio_y_win_rate_son_los_de_las_operaciones_con_el_factor():
    trades = [_trade("10", dia=0), _trade("-4", dia=1), _trade("6", dia=2)]
    factores = [_fo(t, F0, 50 + i) for i, t in enumerate(trades)]
    a = _por_nombre(analyze_factors(trades, factores))[F0]
    assert a.resultado_promedio_usd == D("4") and a.win_rate_pct.quantize(D("0.01")) == D("66.67")


def test_contribucion_es_expectancy_tercio_alto_menos_bajo_del_puntaje_del_factor():
    trades, factores = _muestra(9)
    a = _por_nombre(analyze_factors(trades, factores))[F0]
    # el puntaje del factor baja con el índice y el P&L también: alto = primeras 3, bajo = últimas 3
    esperado_alto = sum((t.net_pnl for t in trades[:3]), D(0)) / 3
    esperado_bajo = sum((t.net_pnl for t in trades[-3:]), D(0)) / 3
    assert a.tercil_alto.n == a.tercil_bajo.n == 3
    assert a.tercil_alto.expectancy_usd == esperado_alto.quantize(D("1E-8"))
    assert a.tercil_bajo.expectancy_usd == esperado_bajo.quantize(D("1E-8"))
    assert a.contribucion_usd == (esperado_alto - esperado_bajo).quantize(D("1E-6"))
    assert a.estado == EstadoValidacion.PRELIMINAR and a.discrimina is True
    assert any("Preliminar" in n for n in a.notas)


def test_un_factor_con_el_mismo_puntaje_en_todas_no_muestra_una_contribucion_inventada():
    trades, factores = _muestra(30)  # los otros 3 factores puntúan 50 en TODAS las operaciones
    plano = _por_nombre(analyze_factors(trades, factores))[F1]
    assert plano.n_operaciones == 30 and plano.estado == EstadoValidacion.EVALUABLE
    # sin esto, el desempate por fecha de entrada daría una "contribución" que no viene del factor
    assert plano.contribucion_usd is None and plano.ci_lower is None and plano.discrimina is None
    assert plano.tercil_alto is None and plano.tercil_bajo is None
    assert any("mismo puntaje en todas" in n for n in plano.notas)
    # pero la frecuencia y el resultado promedio siguen siendo reales
    assert plano.resultado_promedio_usd is not None and plano.pct_de_operaciones == D("100.00")


def test_avisa_si_hay_puntajes_repetidos_en_el_limite_entre_terciles():
    trades = [_trade(str(10 - i), dia=i) for i in range(9)]
    puntajes = [90, 80, 70, 60, 60, 60, 30, 20, 10]  # el medio (60) empata con el límite del tercio alto? no: 70 vs 60
    factores = [_fo(t, F0, p) for t, p in zip(trades, puntajes)]
    sin_empate = _por_nombre(analyze_factors(trades, factores))[F0]
    assert not any("repetidos en el límite" in n for n in sin_empate.notas)

    puntajes = [90, 80, 70, 70, 60, 50, 30, 20, 10]  # el tercio alto termina en 70 y el medio empieza en 70
    factores = [_fo(t, F0, p) for t, p in zip(trades, puntajes)]
    con_empate = _por_nombre(analyze_factors(trades, factores))[F0]
    assert any("repetidos en el límite" in n for n in con_empate.notas)


def test_usa_el_mismo_metodo_que_la_validacion_del_score():
    trades, factores = _muestra(12)
    a = _por_nombre(analyze_factors(trades, factores))[F0]
    equivalentes = [dataclasses.replace(t, initial_opportunity_score=D(90 - i)) for i, t in enumerate(trades)]
    ref = validate_score_discrimination(equivalentes)
    assert (a.contribucion_usd, a.ci_lower, a.ci_upper, a.discrimina) == (
        ref.score_discrimination, ref.ci_lower, ref.ci_upper, ref.discriminates,
    )


def test_confianza_cuenta_las_etiquetas_guardadas():
    trades = [_trade("1", dia=i) for i in range(5)]
    etiquetas = ["alta", "alta", "media", "baja", None]
    factores = [_fo(t, F2, 50 + i, confianza=e) for i, (t, e) in enumerate(zip(trades, etiquetas))]
    c = _por_nombre(analyze_factors(trades, factores))[F2].confianza
    assert (c.alta, c.media, c.baja, c.sin_dato) == (2, 1, 1, 1)


def test_es_determinista_y_no_depende_del_orden_de_entrada():
    trades, factores = _muestra(12)
    a = analyze_factors(trades, factores)
    b = analyze_factors(list(reversed(trades)), list(reversed(factores)))
    assert a == b
    assert (a.iterations, a.seed) == (BOOTSTRAP_ITERACIONES, BOOTSTRAP_SEMILLA)


def test_un_factor_repetido_para_la_misma_operacion_se_rechaza():
    trades, factores = _muestra(3)
    with pytest.raises(ValueError, match="repetido"):
        analyze_factors(trades, factores + [factores[0]])


def test_factores_desconocidos_se_ignoran_y_se_avisa():
    trades, factores = _muestra(3)
    extra = FactorDeOperacion(trades[0].position_id, "inventado", D("0.1"), D("10"), "alta")
    rep = analyze_factors(trades, factores + [extra])
    assert [a.factor for a in rep.factores] == list(FACTORES)
    assert any("inventado" in n for n in rep.notas)


def test_no_modifica_las_operaciones_recibidas():
    trades, factores = _muestra(6)
    antes = [t.initial_opportunity_score for t in trades]
    analyze_factors(trades, factores)
    assert [t.initial_opportunity_score for t in trades] == antes == [D("60")] * 6


def test_las_iteraciones_minimas_del_plan_se_respetan():
    trades, factores = _muestra(6)
    with pytest.raises(ValueError):
        analyze_factors(trades, factores, iterations=10)
