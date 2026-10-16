"""Diagnósticos exploratorios (app.domain.rules.walk_forward_diagnostics)."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.domain.rules.walk_forward import EstadoTramo, ObservacionBacktest, Veredicto, VentanasWalkForward
from app.domain.rules.walk_forward_diagnostics import (
    FACTORES_DIAGNOSTICO,
    censura_por_tercil,
    con_censuradas_marcadas_a_mercado,
    con_rango_dentro_del_dia,
    con_score_de_factor,
    diagnosticos,
    observaciones_de_prueba,
)

D = Decimal


def _fin(dia: date, dias=2) -> datetime:
    return datetime(dia.year, dia.month, dia.day, tzinfo=timezone.utc) + timedelta(days=dias)


def _o(symbol, dia, score, retorno, **kw) -> ObservacionBacktest:
    return ObservacionBacktest(symbol, dia, D(str(score)), None if retorno is None else D(str(retorno)),
                               None if retorno is None else _fin(dia), **kw)


# ------------------------------------------------------------ transformaciones
def test_score_de_factor_reemplaza_y_omite_los_que_no_lo_tienen():
    dia = date(2026, 1, 1)
    a = _o("A", dia, 50, "0.01", factores=(("rsi", D(10)), ("volumen", D(90))))
    b = _o("B", dia, 60, "0.02")  # sin factores
    r = con_score_de_factor([a, b], "rsi")
    assert [(o.symbol, o.score) for o in r] == [("A", D(10))]
    assert con_score_de_factor([a], "inexistente") == []


def test_rango_dentro_del_dia_percentil_empates_y_minimo():
    dia = date(2026, 1, 1)
    obs = [_o(f"S{i}", dia, s, "0.01") for i, s in enumerate([10, 20, 20, 40])]
    r = {o.symbol: o.score for o in con_rango_dentro_del_dia(obs, minimo_monedas=4)}
    # rangos: 10 -> 0; los dos 20 -> 1,5 (rango medio); 40 -> 3. Dividido entre n-1 = 3.
    assert r == {"S0": D(0), "S1": D("1.5") / 3, "S2": D("1.5") / 3, "S3": D(1)}
    assert con_rango_dentro_del_dia(obs, minimo_monedas=5) == []  # el día no llega al mínimo


def test_rango_dentro_del_dia_cada_dia_aporta_por_igual_a_cada_extremo():
    obs = []
    for d in range(10):
        dia = date(2026, 1, 1) + timedelta(days=d)
        # un día "bueno" (todos suben) y uno "malo" alternan; el score ordena dentro del día
        base = D("0.05") if d % 2 == 0 else D("-0.05")
        obs += [_o(f"S{i}", dia, i * 10, base) for i in range(10)]
    r = con_rango_dentro_del_dia(obs, minimo_monedas=10)
    altos = [o for o in r if o.score >= D("0.8")]
    bajos = [o for o in r if o.score <= D("0.2")]
    assert {o.dia for o in altos} == {o.dia for o in bajos}  # misma mezcla de días en ambos extremos


def test_censuradas_marcadas_a_mercado_pasan_a_tener_retorno():
    dia = date(2026, 1, 1)
    marcado_en = _fin(dia, 5)
    abierta = ObservacionBacktest("A", dia, D(50), None, None, retorno_marcado=D("0.03"), marcado_en=marcado_en)
    sin_marcado = _o("B", dia, 50, None)
    cerrada = _o("C", dia, 50, "0.01", retorno_marcado=D("0.01"), marcado_en=marcado_en)
    r = con_censuradas_marcadas_a_mercado([abierta, sin_marcado, cerrada])
    assert (r[0].retorno_neto, r[0].cierre_en) == (D("0.03"), marcado_en)
    assert r[1].retorno_neto is None  # sin precio de marcado sigue censurada: no se inventa
    assert r[2].retorno_neto == D("0.01")


# --------------------------------------------------------------------- censura
def test_censura_por_tercil_cuenta_en_cada_extremo():
    dia = date(2026, 1, 1)
    obs = [_o(f"S{i}", dia, i, "0.01" if i not in (0, 1, 29) else None) for i in range(30)]
    c = {x.nombre: x for x in censura_por_tercil(obs)}
    assert (c["alto"].n, c["medio"].n, c["bajo"].n) == (10, 10, 10)
    assert (c["alto"].censuradas, c["medio"].censuradas, c["bajo"].censuradas) == (1, 0, 2)
    assert c["bajo"].fraccion == D("0.2")
    assert censura_por_tercil(obs[:2]) == ()


def test_observaciones_de_prueba_solo_las_de_ventanas_de_prueba():
    obs = [_o("A", date(2025, 1, 1) + timedelta(days=i), 50, "0.01") for i in range(500)]
    prueba = observaciones_de_prueba(obs, VentanasWalkForward())
    assert prueba and min(o.dia for o in prueba) == date(2026, 1, 1) and max(o.dia for o in prueba) < date(2026, 4, 1)
    assert observaciones_de_prueba([], VentanasWalkForward()) == []


# ------------------------------------------------------------ de punta a punta
def _universo(*, factor_predictivo: str | None, dias=640, monedas=10):
    rng = random.Random(5)
    inicio = date(2025, 1, 1)
    obs = []
    for d in range(dias):
        dia = inicio + timedelta(days=d)
        for s in range(monedas):
            sub = {f: D(rng.randint(0, 100)) for f in FACTORES_DIAGNOSTICO}
            ruido = D(rng.randint(-50, 50)) / 1000
            retorno = ruido + ((sub[factor_predictivo] - 50) / 500 if factor_predictivo else 0)
            score = sum(sub.values(), D(0)) / 4
            obs.append(
                ObservacionBacktest(f"S{s}", dia, score, retorno, _fin(dia),
                                    factores=tuple(sub.items()), retorno_marcado=retorno, marcado_en=_fin(dia))
            )
    return obs


def test_el_diagnostico_por_factor_senala_al_factor_que_realmente_predice():
    inf = diagnosticos(_universo(factor_predictivo="rsi"))
    assert inf.por_factor["rsi"].veredicto == Veredicto.DISCRIMINA
    assert inf.por_factor["volumen"].veredicto == Veredicto.NO_DISCRIMINA
    assert inf.por_factor["tendencia"].veredicto == Veredicto.NO_DISCRIMINA
    assert set(inf.por_factor) == set(FACTORES_DIAGNOSTICO)


def test_el_diagnostico_completo_corre_y_usa_las_ventanas_pedidas():
    inf = diagnosticos(_universo(factor_predictivo=None))
    assert len(inf.rango_dentro_del_dia.pliegues) == 3  # 640 días con 12/3/3
    assert len(inf.sensibilidad_ventanas.pliegues) >= 3 and inf.sensibilidad_ventanas.ventanas.entrenamiento_meses == 9
    assert inf.con_censuradas_marcadas.prueba_agregada.n > 0
    assert len(inf.censura) == 3
    # solo se mide fuera de muestra: el entrenamiento no se calcula
    assert all(r.entrenamiento.estado == EstadoTramo.SIN_DATOS for r in inf.rango_dentro_del_dia.pliegues)
