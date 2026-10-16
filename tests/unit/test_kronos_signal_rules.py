"""Reglas puras de la señal de Kronos (E6): resumen de trayectorias, parámetros
y métricas de contraste. Sin red, sin base y sin torch."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.rules.kronos_signal import (
    CORTE_PREENTRENAMIENTO,
    ParametrosKronos,
    PuntoContraste,
    rank_ic_diario,
    resumir_trayectorias,
    sanidad_vs_persistencia,
    semilla_de_contexto,
    spearman,
    validar_parametros,
)

D = Decimal


def _params(**kw) -> ParametrosKronos:
    base = dict(
        model_id="NeoQuasar/Kronos-small", tokenizer_id="NeoQuasar/Kronos-Tokenizer-base",
        weights_revision="a" * 40, tokenizer_revision="b" * 40, code_commit="c" * 40,
        lookback=400, horizon=24, n_trajectories=8, temperature=D("1.0"), top_p=D("0.9"), seed=1,
    )
    base.update(kw)
    return ParametrosKronos(**base)


# --- parámetros ---


def test_parametros_validos_pasan():
    validar_parametros(_params())


def test_lookback_mas_horizonte_no_puede_pasar_del_contexto_del_modelo():
    validar_parametros(_params(lookback=488, horizon=24))  # justo 512
    with pytest.raises(ValueError, match="excede"):
        validar_parametros(_params(lookback=489, horizon=24))


@pytest.mark.parametrize("campo", ["weights_revision", "tokenizer_revision"])
@pytest.mark.parametrize("valor", ["main", "master", "latest", ""])
def test_la_revision_debe_ser_un_hash_fijo_no_una_rama(campo, valor):
    with pytest.raises(ValueError, match="hash fijo"):
        validar_parametros(_params(**{campo: valor}))


def test_valores_invalidos_se_rechazan():
    for kw in ({"n_trajectories": 0}, {"horizon": 0}, {"lookback": 0}, {"temperature": D(0)}, {"top_p": D(0)}, {"top_p": D("1.1")}):
        with pytest.raises(ValueError):
            validar_parametros(_params(**kw))


def test_el_corte_es_el_primero_de_julio_de_2024_utc():
    assert (CORTE_PREENTRENAMIENTO.year, CORTE_PREENTRENAMIENTO.month, CORTE_PREENTRENAMIENTO.day) == (2024, 7, 1)
    assert CORTE_PREENTRENAMIENTO.utcoffset().total_seconds() == 0


# --- resumen de trayectorias ---


def test_resumen_calcula_media_fraccion_al_alza_y_dispersion():
    r = resumir_trayectorias(D(100), [D(110), D(90), D(105), D(95)])
    assert r.n == 4
    assert r.expected_return == D("0.000000")  # (+10 -10 +5 -5)/4 = 0
    assert r.prob_up == D("0.500000")
    assert r.dispersion == D("0.091287")  # desv. muestral (n-1) de {0.10,-0.10,0.05,-0.05}


def test_resumen_con_una_sola_trayectoria_no_inventa_dispersion():
    r = resumir_trayectorias(D(200), [D(210)])
    assert (r.n, r.expected_return, r.prob_up, r.dispersion) == (1, D("0.050000"), D("1.000000"), None)


def test_un_cierre_igual_al_ultimo_no_cuenta_como_alza():
    assert resumir_trayectorias(D(100), [D(100), D(101)]).prob_up == D("0.500000")


@pytest.mark.parametrize("ultimo,finales", [(D(0), [D(1)]), (D(-1), [D(1)]), (D(1), []), (D(1), [D(0)]), (D(1), [D(1), D(-2)])])
def test_datos_invalidos_fallan_en_vez_de_estimarse(ultimo, finales):
    with pytest.raises(ValueError):
        resumir_trayectorias(ultimo, finales)


# --- sanidad vs persistencia ---


def _p(dia, esperado, realizado, symbol="X"):
    return PuntoContraste(symbol, dia, D(esperado), D(realizado))


def test_sanidad_kronos_mejor_que_persistencia_cuando_su_error_es_menor():
    s = sanidad_vs_persistencia([_p("d1", "0.02", "0.03"), _p("d1", "-0.01", "-0.02")])
    assert s.mae_kronos == D("0.010000") and s.mae_persistencia == D("0.025000")
    assert s.kronos_mejor_que_persistencia is True
    assert s.acierto_direccion == D("1.000000") and s.tasa_base_alza == D("0.500000")


def test_sanidad_persistencia_gana_si_el_pronostico_se_aleja():
    s = sanidad_vs_persistencia([_p("d1", "0.10", "0.01"), _p("d1", "-0.10", "-0.01")])
    assert s.kronos_mejor_que_persistencia is False


def test_realizado_cero_no_cuenta_para_la_direccion():
    s = sanidad_vs_persistencia([_p("d1", "0.01", "0"), _p("d1", "0.01", "0.02")])
    assert s.acierto_direccion == D("1.000000") and s.tasa_base_alza == D("1.000000")
    assert sanidad_vs_persistencia([_p("d1", "0.01", "0")]).acierto_direccion is None


def test_sanidad_sin_puntos_falla():
    with pytest.raises(ValueError):
        sanidad_vs_persistencia([])


# --- Spearman y RankIC diario ---


def test_spearman_perfecto_inverso_y_empates():
    xs = [D(1), D(2), D(3), D(4)]
    assert spearman(xs, [D(10), D(20), D(30), D(40)]) == D("1.000000")
    assert spearman(xs, [D(40), D(30), D(20), D(10)]) == D("-1.000000")
    # con empates se usan rangos medios y sigue estando definido
    assert spearman([D(1), D(1), D(2), D(3)], [D(1), D(2), D(3), D(4)]) is not None


def test_spearman_no_definido_se_devuelve_none_no_cero():
    assert spearman([D(1), D(1), D(1)], [D(1), D(2), D(3)]) is None  # sin variación
    assert spearman([D(1), D(2)], [D(1), D(2)]) is None  # menos de 3 pares
    with pytest.raises(ValueError):
        spearman([D(1)], [D(1), D(2)])


def _dia(dia, n, invertido=False):
    return [_p(dia, str(i), str(-i if invertido else i), symbol=f"S{i}") for i in range(n)]


def test_rank_ic_promedia_por_dia_y_excluye_dias_con_pocas_monedas():
    puntos = _dia("d1", 10) + _dia("d2", 10, invertido=True) + _dia("d3", 5)
    r = rank_ic_diario(puntos, minimo_por_dia=10)
    assert (r.dias_usados, r.dias_excluidos) == (2, 1)
    assert r.promedio == D("0.000000")  # +1 y -1


def test_rank_ic_sin_dias_utilizables_es_none():
    r = rank_ic_diario(_dia("d1", 4), minimo_por_dia=10)
    assert (r.dias_usados, r.dias_excluidos, r.promedio) == (0, 1, None)


# --- semilla por contexto (el pronóstico no debe depender de sus acompañantes) ---

_T0 = datetime(2025, 3, 1, tzinfo=timezone.utc)


def test_la_semilla_de_contexto_es_estable_y_valida_para_torch():
    a = semilla_de_contexto(20261015, "BTCUSDT", _T0)
    assert a == semilla_de_contexto(20261015, "BTCUSDT", _T0)
    assert 0 <= a < 2**63
    # Valor fijado: si cambia, cambiarían TODOS los pronósticos ya generados (reproducibilidad).
    assert a == 2764189098335916757
    # El mismo instante en otra zona horaria da la misma semilla.
    assert a == semilla_de_contexto(20261015, "BTCUSDT", _T0.astimezone(timezone(timedelta(hours=-5))))


def test_la_semilla_cambia_con_cada_componente():
    base = semilla_de_contexto(1, "BTCUSDT", _T0)
    assert semilla_de_contexto(2, "BTCUSDT", _T0) != base
    assert semilla_de_contexto(1, "ETHUSDT", _T0) != base
    assert semilla_de_contexto(1, "BTCUSDT", _T0 + timedelta(days=1)) != base


def test_la_semilla_exige_zona_horaria():
    with pytest.raises(ValueError):
        semilla_de_contexto(1, "BTCUSDT", datetime(2025, 3, 1))
