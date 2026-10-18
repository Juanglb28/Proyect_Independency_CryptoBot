"""app.domain.rules.intravela: comparación pura de las dos lecturas del orden intravela."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.domain.rules.intravela import comparar_ordenes
from app.domain.rules.walk_forward import ObservacionBacktest

D = Decimal
_CIERRE = datetime(2026, 2, 1, tzinfo=timezone.utc)


def _o(symbol: str, dia: int, retorno: str | None, score: str = "50") -> ObservacionBacktest:
    return ObservacionBacktest(
        symbol=symbol, dia=date(2026, 1, dia), score=D(score),
        retorno_neto=None if retorno is None else D(retorno),
        cierre_en=None if retorno is None else _CIERRE,
    )


def test_compara_medias_y_cuenta_quien_gana_y_pierde():
    base = [_o("A", 1, "0.10"), _o("B", 1, "0.05"), _o("C", 1, "-0.08"), _o("D", 1, "0.02")]
    inversa = [_o("A", 1, "0.04"), _o("B", 1, "0.05"), _o("C", 1, "-0.08"), _o("D", 1, "0.06")]
    c = comparar_ordenes(base, inversa)
    assert c.n_total == 4 and c.n_resueltas_ambas == 4
    assert c.media_base == D("0.0225") and c.media_inversa == D("0.0175")
    assert c.diferencia_media == D("-0.005")
    assert c.n_cambian == 2 and c.pct_cambian == D("0.5")
    assert c.n_inversa_mejor == 1 and c.n_inversa_peor == 1
    assert c.mayor_ganancia_inversa == D("0.04") and c.mayor_perdida_inversa == D("-0.06")
    assert c.diferencia_media_si_cambian == D("-0.01")


def test_las_censuradas_se_cuentan_y_no_entran_a_las_medias():
    base = [_o("A", 1, "0.10"), _o("B", 1, None), _o("C", 1, None)]
    inversa = [_o("A", 1, "0.10"), _o("B", 1, "-0.08"), _o("C", 1, None)]
    c = comparar_ordenes(base, inversa)
    assert c.censuradas_base == 2 and c.censuradas_inversa == 1
    assert c.n_resueltas_ambas == 1 and c.media_base == D("0.10") and c.n_cambian == 0
    assert c.diferencia_media_si_cambian is None and c.mayor_ganancia_inversa is None


def test_sin_resueltas_en_ambas_no_inventa_cifras():
    c = comparar_ordenes([_o("A", 1, None)], [_o("A", 1, None)])
    assert c.n_resueltas_ambas == 0 and c.media_base is None and c.diferencia_media is None and c.pct_cambian == 0


def test_listas_distintas_o_duplicadas_o_con_score_distinto_son_error():
    with pytest.raises(ValueError, match="mismas observaciones"):
        comparar_ordenes([_o("A", 1, "0.1")], [_o("B", 1, "0.1")])
    with pytest.raises(ValueError, match="duplicadas"):
        comparar_ordenes([_o("A", 1, "0.1"), _o("A", 1, "0.2")], [_o("A", 1, "0.1")])
    with pytest.raises(ValueError, match="score"):
        comparar_ordenes([_o("A", 1, "0.1", "50")], [_o("A", 1, "0.1", "51")])


# -- ubicar_resuelta (resolución con velas de 1 minuto, 2026-10-17) ---------------------------------

from app.domain.rules.intravela import ubicar_resuelta  # noqa: E402


def _obs(dia: int, retorno, score="60"):
    return _o("X", dia, retorno, score)


def test_ubicar_resuelta_clasifica_cada_observacion():
    base = [_obs(1, "0.05"), _obs(2, "0.10"), _obs(3, "0.10"), _obs(4, "0.10"), _obs(5, "0.10"), _obs(6, "0.10"), _obs(7, None)]
    inversa = [_obs(1, "0.05"), _obs(2, "0.02"), _obs(3, "0.02"), _obs(4, "0.02"), _obs(5, "0.10"), _obs(6, "0.10"), _obs(7, "0.01")]
    resuelta = [_obs(1, "0.05"), _obs(2, "0.10"), _obs(3, "0.02"), _obs(4, "0.06"), _obs(5, "-0.08"), _obs(6, "0.12"), _obs(7, "0.01")]
    u = ubicar_resuelta(base, inversa, resuelta)
    assert u.n == 6  # el día 7 está censurado en la base
    assert (u.n_sin_cambio, u.n_igual_base, u.n_igual_inversa, u.n_entre, u.n_fuera) == (1, 1, 1, 1, 2)
    assert u.n == u.n_sin_cambio + u.n_igual_base + u.n_igual_inversa + u.n_entre + u.n_fuera


def test_ubicar_resuelta_exige_las_mismas_observaciones_y_el_mismo_score():
    import pytest

    uno = [_obs(1, "0.01")]
    with pytest.raises(ValueError, match="mismas observaciones"):
        ubicar_resuelta(uno, uno, [_obs(2, "0.01")])
    with pytest.raises(ValueError, match="score"):
        ubicar_resuelta(uno, uno, [_obs(1, "0.01", score="70")])
    with pytest.raises(ValueError, match="duplicadas"):
        ubicar_resuelta(uno, uno, [_obs(1, "0.01"), _obs(1, "0.02")])


def test_ubicar_resuelta_sin_observaciones_resueltas():
    u = ubicar_resuelta([_obs(1, None)], [_obs(1, None)], [_obs(1, None)])
    assert u.n == 0 and u.media_resuelta is None
