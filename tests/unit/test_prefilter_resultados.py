"""`clasificar_resultados` (qué se guarda del pre-filtro de cada ciclo) y la
huella de la versión de fórmula. Puro: sin base de datos."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal

from app.data import prefilter
from app.data.prefilter import PREFILTER_FORMULA_VERSION, PrefilterScore, clasificar_resultados, select_candidates
from app.domain.enums import EstadoPrefiltro

_T = datetime(2026, 10, 6, tzinfo=timezone.utc)


def _score(symbol: str, final: str) -> PrefilterScore:
    f = Decimal(final)
    return PrefilterScore(symbol, _T, Decimal(1), Decimal(1), Decimal(-1), f, f, f, f)


def _clasificar(scores, sin_datos=()):
    return clasificar_resultados(scores, select_candidates(scores), list(sin_datos))


def test_los_cuatro_estados_se_asignan_segun_las_reglas_ya_fijadas():
    # 7 con puntaje >= 45 (mínimo) y tope de 5 -> 2 caen por tope; 1 bajo el mínimo; 1 sin datos.
    scores = [_score(f"S{i}", str(90 - i * 5)) for i in range(7)] + [_score("BAJO", "30")]
    res = {r.symbol: r for r in _clasificar(scores, sin_datos=["NODATA"])}

    assert [res[f"S{i}"].estado for i in range(5)] == [EstadoPrefiltro.CANDIDATO] * 5
    assert res["S5"].estado == EstadoPrefiltro.DESCARTADO_POR_TOPE
    assert res["S6"].estado == EstadoPrefiltro.DESCARTADO_POR_TOPE
    assert res["BAJO"].estado == EstadoPrefiltro.DESCARTADO_POR_PUNTAJE
    assert res["NODATA"].estado == EstadoPrefiltro.SIN_DATOS


def test_ranking_por_puntaje_con_desempate_por_simbolo_y_sin_datos_sin_ranking():
    scores = [_score("B", "70"), _score("A", "70"), _score("C", "80")]
    res = _clasificar(scores, sin_datos=["Z", "Y"])
    assert [(r.symbol, r.ranking) for r in res] == [("C", 1), ("A", 2), ("B", 3), ("Y", None), ("Z", None)]


def test_sin_datos_no_lleva_puntaje_ni_valor_de_relleno():
    (r,) = _clasificar([], sin_datos=["X"])
    assert r.estado == EstadoPrefiltro.SIN_DATOS and r.ranking is None and r.score is None


def test_el_puntaje_exactamente_en_el_minimo_es_candidato():
    (r,) = _clasificar([_score("EXACTO", "45")])
    assert r.estado == EstadoPrefiltro.CANDIDATO


def test_los_candidatos_coinciden_con_select_candidates():
    scores = [_score(f"S{i}", str(95 - i * 7)) for i in range(9)]
    elegidos = {c.symbol for c in select_candidates(scores)}
    clasificados = {r.symbol for r in _clasificar(scores) if r.estado == EstadoPrefiltro.CANDIDATO}
    assert clasificados == elegidos


def test_lista_vacia_da_resultado_vacio():
    assert _clasificar([]) == []


# Si este test falla porque cambió un límite, el umbral o el tope del pre-filtro:
# sube PREFILTER_FORMULA_VERSION y actualiza esta huella. Así un puntaje guardado
# en `prefiltro_resultados` siempre se puede interpretar con la fórmula que lo produjo.
HUELLA_ESPERADA = "e233e9c46f0ae9320774c333419b0042dbf5b40be69b39974fb7dea2c65aa511"


def test_la_huella_de_la_formula_exige_subir_la_version_si_cambia_algo():
    assert PREFILTER_FORMULA_VERSION == "1"
    payload = repr(
        (
            prefilter.TENDENCIA_PCT_BOUNDS,
            prefilter.VOLUMEN_RELATIVO_BOUNDS,
            prefilter.DISTANCIA_MAX_90D_PCT_BOUNDS,
            prefilter.SCORE_MINIMO_PREFILTRO,
            prefilter.MAX_CANDIDATOS_PREFILTRO,
        )
    )
    huella = hashlib.sha256(payload.encode()).hexdigest()
    assert huella == HUELLA_ESPERADA, (
        "La fórmula del pre-filtro cambió: sube PREFILTER_FORMULA_VERSION y actualiza HUELLA_ESPERADA. "
        f"Huella actual: {huella}"
    )


# ---------------------------------------------------------------------------
# Símbolos con posición abierta o entrada pendiente (decisión del 2026-10-26)
# ---------------------------------------------------------------------------


def _clasificar_excluyendo(scores, excluidos, sin_datos=()):
    candidatos = select_candidates(scores, excluir=frozenset(excluidos))
    return clasificar_resultados(scores, candidatos, list(sin_datos), excluidos=frozenset(excluidos))


def test_select_candidates_sin_excluir_es_igual_que_siempre():
    scores = [_score(f"S{i}", str(90 - i * 5)) for i in range(7)]
    assert select_candidates(scores) == select_candidates(scores, excluir=frozenset())
    assert [c.symbol for c in select_candidates(scores)] == ["S0", "S1", "S2", "S3", "S4"]


def test_un_simbolo_excluido_no_es_candidato_y_su_cupo_pasa_al_siguiente():
    # 7 símbolos >= 45: sin exclusión caen por tope S5 y S6. S0 está ocupado: S5 entra y solo S6 cae.
    scores = [_score(f"S{i}", str(90 - i * 5)) for i in range(7)]
    elegidos = [c.symbol for c in select_candidates(scores, excluir=frozenset({"S0"}))]
    assert elegidos == ["S1", "S2", "S3", "S4", "S5"]


def test_clasificar_marca_al_excluido_como_posicion_abierta_y_no_como_tope():
    scores = [_score(f"S{i}", str(90 - i * 5)) for i in range(7)]
    res = {r.symbol: r for r in _clasificar_excluyendo(scores, {"S0"})}

    assert res["S0"].estado == EstadoPrefiltro.DESCARTADO_POR_POSICION_ABIERTA
    assert [res[f"S{i}"].estado for i in range(1, 6)] == [EstadoPrefiltro.CANDIDATO] * 5
    assert res["S6"].estado == EstadoPrefiltro.DESCARTADO_POR_TOPE
    # El puntaje y el ranking del excluido se conservan: la evidencia no se altera.
    assert res["S0"].ranking == 1 and res["S0"].score is not None and res["S0"].score.score_final == Decimal(90)


def test_un_ocupado_bajo_el_minimo_sigue_siendo_descartado_por_puntaje():
    scores = [_score("OCUPADO", "30"), _score("OK", "70")]
    res = {r.symbol: r for r in _clasificar_excluyendo(scores, {"OCUPADO"})}
    assert res["OCUPADO"].estado == EstadoPrefiltro.DESCARTADO_POR_PUNTAJE
    assert res["OK"].estado == EstadoPrefiltro.CANDIDATO


def test_un_ocupado_sin_datos_sigue_siendo_sin_datos():
    (r,) = _clasificar_excluyendo([], {"X"}, sin_datos=["X"])
    assert r.estado == EstadoPrefiltro.SIN_DATOS and r.score is None


def test_el_estado_nuevo_cabe_en_la_columna_de_32_caracteres():
    assert len(EstadoPrefiltro.DESCARTADO_POR_POSICION_ABIERTA.value) <= 32
