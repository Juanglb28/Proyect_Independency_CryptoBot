"""Exclusión de símbolos con posición abierta o entrada pendiente del pre-filtro
(decisión del 2026-10-26: el sistema no aumenta posiciones, así que no se analizan con IA).

Puro: sin base de datos. El ciclo diario lo usa antes de analizar; el ciclo horario, para que un
símbolo ocupado no dispare un ciclo de IA como "candidato nuevo"."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.data.prefilter import PrefilterScore, select_candidates
from app.data.prefilter_pipeline import PrefilterRunResult, excluir_simbolos_ocupados
from app.orchestration.hourly_cycle import _detectar_candidatos_nuevos

_T = datetime(2026, 10, 26, tzinfo=timezone.utc)


def _score(symbol: str, final: str) -> PrefilterScore:
    f = Decimal(final)
    return PrefilterScore(symbol, _T, Decimal(1), Decimal(1), Decimal(-1), f, f, f, f)


def _resultado(scores, sin_datos=()) -> PrefilterRunResult:
    return PrefilterRunResult(
        scores=tuple(scores), candidates=tuple(select_candidates(list(scores))), symbols_sin_datos=tuple(sin_datos)
    )


def test_sin_simbolos_ocupados_devuelve_el_mismo_resultado():
    r = _resultado([_score("A", "80"), _score("B", "70")])
    assert excluir_simbolos_ocupados(r, frozenset()) is r


def test_ocupados_que_no_tienen_puntaje_no_cambian_nada():
    r = _resultado([_score("A", "80")], sin_datos=["SINDATOS"])
    assert excluir_simbolos_ocupados(r, frozenset({"SINDATOS", "OTRO"})) is r


def test_quita_al_ocupado_de_los_candidatos_y_lo_anota():
    r = _resultado([_score("A", "80"), _score("B", "70")])
    nuevo = excluir_simbolos_ocupados(r, frozenset({"A"}))
    assert [c.symbol for c in nuevo.candidates] == ["B"]
    assert nuevo.simbolos_excluidos == frozenset({"A"})


def test_los_puntajes_y_los_sin_datos_no_cambian():
    r = _resultado([_score("A", "80"), _score("B", "70")], sin_datos=["Z"])
    nuevo = excluir_simbolos_ocupados(r, frozenset({"A"}))
    assert nuevo.scores == r.scores
    assert nuevo.symbols_sin_datos == ("Z",)


def test_el_cupo_del_ocupado_pasa_al_siguiente():
    scores = [_score(f"S{i}", str(90 - i * 5)) for i in range(7)]  # 7 con puntaje >= 45; el tope es 5
    r = _resultado(scores)
    assert [c.symbol for c in r.candidates] == ["S0", "S1", "S2", "S3", "S4"]
    nuevo = excluir_simbolos_ocupados(r, frozenset({"S0", "S1"}))
    assert [c.symbol for c in nuevo.candidates] == ["S2", "S3", "S4", "S5", "S6"]


def test_un_ocupado_fuera_del_tope_tambien_queda_anotado_sin_cambiar_los_candidatos():
    scores = [_score(f"S{i}", str(90 - i * 5)) for i in range(7)]
    r = _resultado(scores)
    nuevo = excluir_simbolos_ocupados(r, frozenset({"S6"}))
    assert nuevo.candidates == r.candidates
    assert nuevo.simbolos_excluidos == frozenset({"S6"})


def test_si_todos_los_candidatos_estan_ocupados_no_queda_ninguno():
    r = _resultado([_score("A", "80")])
    assert excluir_simbolos_ocupados(r, frozenset({"A"})).candidates == ()


# --- ciclo horario: un símbolo ocupado nunca es "candidato nuevo" ---


def test_el_ciclo_horario_detecta_el_candidato_nuevo_como_siempre():
    r = _resultado([_score("A", "60")])
    nuevos = _detectar_candidatos_nuevos(r, {"A": Decimal("30")})
    assert [n.symbol for n in nuevos] == ["A"]


def test_el_ciclo_horario_ignora_a_un_simbolo_ocupado():
    r = _resultado([_score("A", "60"), _score("B", "60")])
    nuevos = _detectar_candidatos_nuevos(r, {"A": Decimal("30"), "B": Decimal("30")}, ocupados=frozenset({"A"}))
    assert [n.symbol for n in nuevos] == ["B"]
