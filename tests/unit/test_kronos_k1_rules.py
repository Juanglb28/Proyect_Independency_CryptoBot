"""Reglas de evaluación de K1 (protocolo v1): RankIC diario, bootstrap por bloques,
permutación, terciles/E4 y veredicto. Datos sintéticos; sin red, base ni torch."""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.domain.rules import kronos_k1 as k1
from app.domain.rules.kronos_k1 import (
    ObservacionK1,
    VeredictoK1,
    bootstrap_bloques,
    evaluar_k1,
    ic_diarios,
    p_permutacion,
    terciles_de_senal,
)

D = Decimal
INICIO = date(2024, 7, 1)


def _q(x: float) -> Decimal:
    return Decimal(str(round(x, 6)))


def _mundo(n_fechas=40, n_monedas=12, *, kronos, mom24, mom7, score=None, semilla=1):
    """Construye observaciones con retornos aleatorios y señales definidas por funciones
    (retorno, ruido) -> valor."""
    rng = random.Random(semilla)
    obs = []
    for i in range(n_fechas):
        dia = INICIO + timedelta(days=3 * i)
        for j in range(n_monedas):
            ret = rng.uniform(-0.1, 0.1)
            obs.append(
                ObservacionK1(
                    symbol=f"S{j:02d}USDT", dia=dia, retorno_neto=_q(ret),
                    kronos=_q(kronos(ret, rng)), score_v1=_q((score or (lambda r, g: g.uniform(-1, 1)))(ret, rng)),
                    mom_24h=_q(mom24(ret, rng)), mom_7d=_q(mom7(ret, rng)),
                )
            )
    return obs


ruido = lambda r, g: g.uniform(-1, 1)  # noqa: E731
predice = lambda r, g: r + g.uniform(-0.01, 0.01)  # noqa: E731


def test_los_parametros_del_protocolo_estan_fijos():
    """Cambiarlos tras ver resultados es otro experimento: el protocolo los congela."""
    assert (k1.K1_BLOQUE_FECHAS, k1.K1_MIN_FECHAS, k1.K1_MIN_MONEDAS_POR_FECHA) == (5, 30, 10)
    assert (k1.K1_ITERACIONES, k1.K1_SEMILLA) == (1000, 20261005)
    assert k1.K1_SENSIBILIDAD_DESDE == date(2025, 3, 19)
    assert k1.SENALES == ("kronos", "score_v1", "mom_24h", "mom_7d")


# --- RankIC diario ---------------------------------------------------------------

def test_ic_diario_es_uno_si_la_senal_ordena_igual_que_el_retorno():
    obs = _mundo(3, 12, kronos=lambda r, g: r, mom24=ruido, mom7=ruido)
    assert set(ic_diarios(obs, "kronos").values()) == {D("1")}


def test_las_fechas_con_menos_de_diez_monedas_se_excluyen_sin_rellenar():
    obs = _mundo(3, 12, kronos=lambda r, g: r, mom24=ruido, mom7=ruido)
    ultima = max(o.dia for o in obs)
    obs = [o for o in obs if not (o.dia == ultima and o.symbol >= "S09")]  # deja 9 monedas ese día
    assert ultima not in ic_diarios(obs, "kronos") and len(ic_diarios(obs, "kronos")) == 2


def test_una_senal_constante_no_define_correlacion_y_la_fecha_se_omite():
    obs = _mundo(2, 12, kronos=lambda r, g: 1, mom24=ruido, mom7=ruido)
    assert ic_diarios(obs, "kronos") == {}


# --- bootstrap por bloques ---------------------------------------------------------

def test_bootstrap_de_una_serie_constante_es_degenerado():
    r = bootstrap_bloques([D("0.1")] * 40)
    assert r.media == r.ci_inferior == r.ci_superior == D("0.1")


def test_bootstrap_es_determinista_y_el_intervalo_contiene_la_media_en_un_caso_tipico():
    serie = [D(str(round(random.Random(i).uniform(-0.3, 0.5), 4))) for i in range(60)]
    a, b = bootstrap_bloques(serie), bootstrap_bloques(serie)
    assert a == b and a.ci_inferior <= a.media <= a.ci_superior and a.n == 60


def test_un_bloque_mas_largo_que_la_serie_se_acota():
    r = bootstrap_bloques([D("0.1"), D("0.2"), D("0.3")], bloque=5)
    assert r.n == 3 and r.ci_inferior <= r.media <= r.ci_superior


def test_bootstrap_exige_el_minimo_de_iteraciones_y_datos():
    with pytest.raises(ValueError):
        bootstrap_bloques([D(1)] * 10, iteraciones=999)
    with pytest.raises(ValueError):
        bootstrap_bloques([])


# --- permutación ------------------------------------------------------------------

def test_permutacion_una_senal_perfecta_casi_nunca_se_iguala_por_azar():
    obs = _mundo(10, 12, kronos=lambda r, g: r, mom24=ruido, mom7=ruido)
    fechas = sorted(ic_diarios(obs, "kronos"))
    assert p_permutacion(obs, "kronos", fechas) == D("0")


def test_permutacion_una_senal_aleatoria_tiene_p_grande_y_es_determinista():
    obs = _mundo(10, 12, kronos=ruido, mom24=ruido, mom7=ruido)
    fechas = sorted(ic_diarios(obs, "kronos"))
    p = p_permutacion(obs, "kronos", fechas)
    assert p > D("0.05") and p == p_permutacion(obs, "kronos", fechas)


# --- terciles y E4 ----------------------------------------------------------------

def test_terciles_una_senal_informativa_da_diferencia_positiva_con_ic_sobre_cero():
    obs = _mundo(20, 12, kronos=predice, mom24=ruido, mom7=ruido)
    t = terciles_de_senal(obs)
    assert t is not None and t.k == len(obs) // 3 and t.diferencia > 0 and t.ci_inferior > 0


def test_terciles_e4_excluye_los_meses_con_una_sola_observacion():
    obs = _mundo(20, 12, kronos=predice, mom24=ruido, mom7=ruido)
    solitaria = ObservacionK1("S00USDT", date(2030, 1, 1), D("0.1"), D("0.1"), D("0.1"), D("0.1"), D("0.1"))
    t = terciles_de_senal(obs + [solitaria], centrar_por_mes=True)
    assert t is not None and t.estratos_excluidos == 1 and t.observaciones_excluidas == 1


def test_terciles_con_menos_de_tres_observaciones_no_se_calculan():
    assert terciles_de_senal(_mundo(1, 2, kronos=predice, mom24=ruido, mom7=ruido)) is None


# --- veredicto --------------------------------------------------------------------

def test_discrimina_y_anade_si_kronos_predice_y_supera_a_los_dos_momentum():
    informe = evaluar_k1(_mundo(40, 12, kronos=predice, mom24=ruido, mom7=ruido))
    assert informe.veredicto is VeredictoK1.DISCRIMINA_Y_ANADE
    assert informe.ic["kronos"].ci_inferior > 0 and informe.n_fechas == 40
    assert informe.dif_vs_mom_24h.ci_inferior > 0 and informe.dif_vs_mom_7d.ci_inferior > 0


def test_discrimina_pero_no_anade_si_un_momentum_hace_lo_mismo():
    informe = evaluar_k1(_mundo(40, 12, kronos=lambda r, g: r, mom24=lambda r, g: r, mom7=ruido))
    assert informe.ic["kronos"].ci_inferior > 0
    assert informe.veredicto is VeredictoK1.DISCRIMINA_NO_ANADE  # diferencia pareada con 24 h = 0


def test_se_compara_contra_cada_momentum_no_contra_el_mejor():
    """Si Kronos solo supera a uno de los dos, NO añade."""
    informe = evaluar_k1(_mundo(40, 12, kronos=predice, mom24=ruido, mom7=lambda r, g: r + g.uniform(-0.001, 0.001)))
    assert informe.veredicto is VeredictoK1.DISCRIMINA_NO_ANADE


def test_no_discrimina_con_una_senal_sin_informacion():
    informe = evaluar_k1(_mundo(40, 12, kronos=ruido, mom24=ruido, mom7=ruido))
    assert informe.veredicto is VeredictoK1.NO_DISCRIMINA


def test_una_senal_invertida_tampoco_discrimina():
    informe = evaluar_k1(_mundo(40, 12, kronos=lambda r, g: -r, mom24=ruido, mom7=ruido))
    assert informe.veredicto is VeredictoK1.NO_DISCRIMINA and informe.ic["kronos"].media < 0


def test_con_menos_de_treinta_fechas_es_no_concluyente_aunque_la_senal_sea_perfecta():
    informe = evaluar_k1(_mundo(29, 12, kronos=lambda r, g: r, mom24=ruido, mom7=ruido))
    assert informe.veredicto is VeredictoK1.NO_CONCLUYENTE and informe.n_fechas == 29
    assert any("NO_CONCLUYENTE" in n for n in informe.notas)


def test_sin_observaciones_es_no_concluyente_sin_inventar_cifras():
    informe = evaluar_k1([])
    assert informe.veredicto is VeredictoK1.NO_CONCLUYENTE and informe.ic == {} and informe.p_permutacion is None


def test_las_fechas_se_comparan_en_comun_para_las_cuatro_senales():
    """Si una señal no tiene correlación definida en una fecha, esa fecha sale para TODAS."""
    obs = _mundo(40, 12, kronos=predice, mom24=ruido, mom7=ruido)
    dia = obs[0].dia
    obs = [o if o.dia != dia else ObservacionK1(o.symbol, o.dia, o.retorno_neto, o.kronos, o.score_v1, D("0.5"), o.mom_7d) for o in obs]
    informe = evaluar_k1(obs)
    assert informe.n_fechas == 39 and informe.fechas_excluidas == 1


def test_la_correlacion_con_el_score_es_alta_si_son_la_misma_senal():
    informe = evaluar_k1(_mundo(40, 12, kronos=lambda r, g: r, score=lambda r, g: r, mom24=ruido, mom7=ruido))
    assert informe.corr_kronos_score == D("1")
