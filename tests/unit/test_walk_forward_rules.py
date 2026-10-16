"""Reglas puras del walk-forward (app.domain.rules.walk_forward)."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.rules.results import bootstrap_diferencia_de_terciles
from app.domain.rules.walk_forward import (
    EstadoTramo,
    ObservacionBacktest,
    Veredicto,
    VentanasWalkForward,
    evaluar_tramo,
    generar_pliegues,
    informe_walk_forward,
    sumar_meses,
)

D = Decimal


def _obs(dia: date, score, retorno, *, symbol="BTCUSDT", dias_hasta_cierre=2) -> ObservacionBacktest:
    cierre = None
    if retorno is not None:
        cierre = datetime(dia.year, dia.month, dia.day, tzinfo=timezone.utc) + timedelta(days=dias_hasta_cierre)
    return ObservacionBacktest(symbol, dia, D(str(score)), None if retorno is None else D(str(retorno)), cierre)


# --------------------------------------------------------------- calendario
def test_sumar_meses_ajusta_fin_de_mes_y_cruza_anios():
    assert sumar_meses(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert sumar_meses(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert sumar_meses(date(2026, 11, 15), 3) == date(2027, 2, 15)
    assert sumar_meses(date(2026, 3, 1), 0) == date(2026, 3, 1)


def test_ventanas_por_defecto_son_12_3_3_y_se_validan():
    v = VentanasWalkForward()
    assert (v.entrenamiento_meses, v.prueba_meses, v.paso_meses) == (12, 3, 3)
    for malo in ((0, 3, 3), (12, 0, 3), (12, 3, 0)):
        with pytest.raises(ValueError):
            VentanasWalkForward(*malo)


def test_dos_anios_completos_dan_cuatro_pliegues_sin_solape_de_prueba():
    pl = generar_pliegues(date(2025, 1, 1), date(2026, 12, 31), VentanasWalkForward())
    assert [p.prueba_desde for p in pl] == [date(2026, 1, 1), date(2026, 4, 1), date(2026, 7, 1), date(2026, 10, 1)]
    assert pl[-1].prueba_hasta == date(2027, 1, 1)
    for a, b in zip(pl, pl[1:]):
        assert a.prueba_hasta == b.prueba_desde  # paso == prueba: contiguas, sin solape


def test_la_calentada_de_indicadores_reduce_los_pliegues():
    # ~18 meses elegibles (2 años menos ~6 meses de SMA180): solo caben 2.
    pl = generar_pliegues(date(2025, 7, 1), date(2026, 12, 31), VentanasWalkForward())
    assert len(pl) == 2


def test_un_tramo_de_prueba_incompleto_no_se_usa():
    # Historial: 12 m + 3 m menos un día -> ningún pliegue.
    assert generar_pliegues(date(2025, 1, 1), date(2026, 3, 30), VentanasWalkForward()) == []
    assert len(generar_pliegues(date(2025, 1, 1), date(2026, 3, 31), VentanasWalkForward())) == 1


# ------------------------------------------------------------------- tramo
def _tramo_monotono(n=60):
    base = date(2026, 1, 1)
    # score sube con el retorno: relación perfecta
    return [_obs(base + timedelta(days=i % 30), score=i, retorno=D(i) / 1000, symbol=f"S{i}") for i in range(n)]


def test_relacion_perfecta_discrimina():
    t = evaluar_tramo(_tramo_monotono())
    assert t.estado == EstadoTramo.EVALUABLE and t.discrimina is True
    assert t.discriminacion > 0 and t.ci_inferior > 0 and t.tercil_size == 20
    assert t.expectancy_alta > t.expectancy_baja


def test_relacion_invertida_no_discrimina():
    obs = [_obs(date(2026, 1, 1) + timedelta(days=i % 30), score=i, retorno=D(-i) / 1000, symbol=f"S{i}") for i in range(60)]
    t = evaluar_tramo(obs)
    assert t.discrimina is False and t.discriminacion < 0


def test_el_resultado_no_depende_del_orden_de_la_lista():
    obs = _tramo_monotono()
    barajada = obs[:]
    random.Random(7).shuffle(barajada)
    assert evaluar_tramo(obs) == evaluar_tramo(barajada)


def test_censuradas_se_cuentan_y_no_entran_a_la_comparacion():
    obs = _tramo_monotono(30) + [_obs(date(2026, 2, 1), 99, None, symbol="CENS")]
    t = evaluar_tramo(obs)
    assert t.n == 30 and t.censuradas == 1


def test_muestras_chicas_se_declaran_no_se_rellenan():
    assert evaluar_tramo(_tramo_monotono(2)).estado == EstadoTramo.SIN_DATOS
    t = evaluar_tramo(_tramo_monotono(29))
    assert t.estado == EstadoTramo.PRELIMINAR
    assert evaluar_tramo(_tramo_monotono(30)).estado == EstadoTramo.EVALUABLE
    assert evaluar_tramo([]).discrimina is None


def test_usa_el_nucleo_unico_del_bootstrap():
    obs = _tramo_monotono()
    ordenadas = sorted(obs, key=lambda o: (-o.score, o.dia, o.symbol))
    boot = bootstrap_diferencia_de_terciles([o.retorno_neto for o in ordenadas])
    t = evaluar_tramo(obs)
    assert t.ci_inferior == boot.ci_lower.quantize(D("1E-8")) and t.ci_superior == boot.ci_upper.quantize(D("1E-8"))


# ------------------------------------------------------------------ informe
def _universo(inicio: date, dias: int, *, relacion: int, simbolos=5, dias_hasta_cierre=2):
    """`relacion` > 0: el score predice el retorno; < 0: al revés; 0: ruido fijo."""
    rng = random.Random(11)
    obs = []
    for d in range(dias):
        for s in range(simbolos):
            score = D(rng.randint(0, 100))
            ruido = D(rng.randint(-50, 50)) / 1000
            retorno = ruido + (score - 50) / 1000 * relacion if relacion else ruido
            obs.append(
                ObservacionBacktest(
                    f"S{s}", inicio + timedelta(days=d), score, retorno,
                    datetime.combine(inicio + timedelta(days=d + dias_hasta_cierre), datetime.min.time(), tzinfo=timezone.utc),
                )
            )
    return obs


def test_score_con_poder_predictivo_da_discrimina_fuera_de_muestra():
    inf = informe_walk_forward(_universo(date(2025, 1, 1), 640, relacion=2), VentanasWalkForward())
    assert len(inf.pliegues) == 3
    assert inf.veredicto == Veredicto.DISCRIMINA
    assert inf.prueba_agregada.discrimina is True
    assert inf.pliegues_con_diferencia_positiva == 3
    assert all(r.entrenamiento.discrimina for r in inf.pliegues)  # y también dentro de la muestra


def test_score_sin_relacion_no_discrimina():
    inf = informe_walk_forward(_universo(date(2025, 1, 1), 640, relacion=0), VentanasWalkForward())
    assert inf.veredicto == Veredicto.NO_DISCRIMINA


def test_relacion_invertida_fuera_de_muestra_no_discrimina():
    inf = informe_walk_forward(_universo(date(2025, 1, 1), 640, relacion=-2), VentanasWalkForward())
    assert inf.veredicto == Veredicto.NO_DISCRIMINA
    assert inf.pliegues_con_diferencia_positiva == 0


def test_sin_historial_suficiente_se_declara_sin_pliegues():
    inf = informe_walk_forward(_universo(date(2025, 1, 1), 100, relacion=2), VentanasWalkForward())
    assert inf.veredicto == Veredicto.SIN_PLIEGUES and inf.pliegues == () and inf.prueba_agregada is None
    assert any("no alcanza" in n for n in inf.notas)
    assert informe_walk_forward([]).veredicto == Veredicto.SIN_PLIEGUES


def test_muestra_agregada_menor_a_30_es_insuficiente_aunque_el_ic_sea_positivo():
    # 1 moneda, ventanas de 1 mes: la prueba (febrero) tiene 28 observaciones.
    inf = informe_walk_forward(_universo(date(2025, 1, 1), 60, relacion=5, simbolos=1), VentanasWalkForward(1, 1, 1))
    assert len(inf.pliegues) == 1 and inf.prueba_agregada.n == 28
    assert inf.prueba_agregada.estado == EstadoTramo.PRELIMINAR
    assert inf.veredicto == Veredicto.MUESTRA_INSUFICIENTE


def test_purga_excluye_del_entrenamiento_lo_que_se_resuelve_en_la_prueba():
    inicio = date(2025, 1, 1)
    obs = _universo(inicio, 640, relacion=2, simbolos=2, dias_hasta_cierre=2)
    # Una observación del último día de entrenamiento del pliegue 1 que cierra DENTRO de la prueba:
    ultimo_dia_entreno = date(2025, 12, 31)
    obs.append(
        ObservacionBacktest("PURGA", ultimo_dia_entreno, D(50), D("0.01"),
                            datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc) + timedelta(hours=1))
    )
    # y otra que cierra justo a las 00:00 del primer día de prueba (permitida: <=)
    obs.append(
        ObservacionBacktest("LIMITE", ultimo_dia_entreno, D(50), D("0.01"), datetime(2026, 1, 1, tzinfo=timezone.utc))
    )
    base = informe_walk_forward(obs[:-2], VentanasWalkForward())
    con = informe_walk_forward(obs, VentanasWalkForward())
    p1_base, p1_con = base.pliegues[0].entrenamiento, con.pliegues[0].entrenamiento
    # las 2 monedas del universo cierran a +2 días: sus 2 observaciones del 30 y 31 dic también caen en prueba
    assert p1_con.purgadas == p1_base.purgadas + 1
    assert p1_con.n == p1_base.n + 1  # LIMITE entra, PURGA no


def test_ventanas_de_prueba_solapadas_no_duplican_observaciones_en_la_agregada():
    obs = _universo(date(2025, 1, 1), 640, relacion=2, simbolos=2)
    solapadas = informe_walk_forward(obs, VentanasWalkForward(12, 3, 1))
    assert len(solapadas.pliegues) > 3
    dias_prueba = {
        o.dia for o in obs
        if solapadas.pliegues[0].pliegue.prueba_desde <= o.dia < solapadas.pliegues[-1].pliegue.prueba_hasta
    }
    assert solapadas.prueba_agregada.n + solapadas.prueba_agregada.censuradas == len(dias_prueba) * 2
    assert any("se solapan" in n for n in solapadas.notas)


def test_el_informe_es_reproducible():
    obs = _universo(date(2025, 1, 1), 640, relacion=1)
    assert informe_walk_forward(obs) == informe_walk_forward(list(reversed(obs)))


def test_las_notas_declaran_las_limitaciones():
    inf = informe_walk_forward(_universo(date(2025, 1, 1), 640, relacion=2))
    texto = " ".join(inf.notas)
    assert "optimista" in texto and "DETERMINISTA" in texto and "no calibra nada" in texto


def test_observacion_valida_su_consistencia():
    with pytest.raises(ValueError):
        ObservacionBacktest("X", date(2026, 1, 1), D(1), D("0.1"), None)
    with pytest.raises(ValueError):
        ObservacionBacktest("X", date(2026, 1, 1), D(1), D("0.1"), datetime(2026, 1, 2))  # sin zona


def test_hasta_fecha_deja_solo_decisiones_anteriores_y_no_toca_el_resto():
    from app.domain.rules.walk_forward import hasta_fecha

    obs = [_obs(date(2025, 1, 1) + timedelta(days=i), 50, "0.01", symbol=f"S{i}") for i in range(10)]
    r = hasta_fecha(obs, date(2025, 1, 6))
    assert [o.dia for o in r] == [date(2025, 1, 1) + timedelta(days=i) for i in range(5)]
    assert r == obs[:5]  # mismos objetos, sin transformar
    assert hasta_fecha(obs, date(2025, 1, 1)) == []
