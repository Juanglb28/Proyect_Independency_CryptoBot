"""Tests de app.domain.rules.execution.select_entries (Paso 8, decisiones del
2026-10-04): orden de prioridad, consumo de cupos (con reserva de entradas
pendientes), tope propio de mayor riesgo y determinismo."""

from __future__ import annotations

import itertools
import uuid
from decimal import Decimal

from app.domain.enums import TierEntrada
from app.domain.rules.execution import CandidatoEntrada, select_entries
from app.domain.rules.risk import MotivoRechazoEntrada, SituacionDeCupos

D = Decimal


def _c(symbol: str, puntaje: str, tier: TierEntrada = TierEntrada.NORMAL, n: int = 1) -> CandidatoEntrada:
    return CandidatoEntrada(
        proposal_id=uuid.UUID(int=n), symbol=symbol, tier=tier, puntaje_ponderado=D(puntaje)
    )


def _cupos(**overrides) -> SituacionDeCupos:
    defaults = dict(
        posiciones_abiertas=0, entradas_hoy=0, mayor_riesgo_abiertas=0,
        simbolos_abiertos=frozenset(), sistema_pausado=False,
    )
    defaults.update(overrides)
    return SituacionDeCupos(**defaults)


def _simbolos(seleccion) -> list[str]:
    return [s.candidato.symbol for s in seleccion.seleccionadas]


def test_sin_candidatos_no_selecciona_nada():
    r = select_entries([], _cupos())
    assert r.seleccionadas == () and r.descartadas == ()


def test_ordena_por_puntaje_de_mayor_a_menor():
    r = select_entries([_c("ETHUSDT", "60", n=1), _c("BTCUSDT", "70", n=2), _c("SOLUSDT", "65", n=3)], _cupos())
    assert _simbolos(r) == ["BTCUSDT", "SOLUSDT", "ETHUSDT"]


def test_las_normales_entran_antes_que_las_de_mayor_riesgo_aunque_tengan_menor_puntaje():
    r = select_entries(
        [_c("ETHUSDT", "54", TierEntrada.MAYOR_RIESGO, 1), _c("BTCUSDT", "56", TierEntrada.NORMAL, 2)], _cupos()
    )
    assert _simbolos(r) == ["BTCUSDT", "ETHUSDT"]


def test_maximo_3_entradas_por_dia_con_las_mejores_primero():
    candidatos = [_c(s, p, n=i) for i, (s, p) in enumerate(
        [("BTCUSDT", "80"), ("ETHUSDT", "70"), ("SOLUSDT", "60"), ("ADAUSDT", "58")], start=1)]
    r = select_entries(candidatos, _cupos())
    assert _simbolos(r) == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert [d.candidato.symbol for d in r.descartadas] == ["ADAUSDT"]
    assert r.descartadas[0].motivos == (MotivoRechazoEntrada.MAXIMO_ENTRADAS_DIA,)


def test_las_entradas_ya_hechas_hoy_reducen_el_cupo_disponible():
    r = select_entries([_c("BTCUSDT", "80", n=1), _c("ETHUSDT", "70", n=2)], _cupos(entradas_hoy=2))
    assert _simbolos(r) == ["BTCUSDT"]
    assert r.descartadas[0].motivos == (MotivoRechazoEntrada.MAXIMO_ENTRADAS_DIA,)


def test_maximo_4_posiciones_abiertas():
    r = select_entries(
        [_c("BTCUSDT", "80", n=1), _c("ETHUSDT", "70", n=2)],
        _cupos(posiciones_abiertas=3, simbolos_abiertos=frozenset({"ADAUSDT", "SOLUSDT", "XRPUSDT"})),
    )
    assert _simbolos(r) == ["BTCUSDT"]
    assert r.descartadas[0].motivos == (MotivoRechazoEntrada.MAXIMO_POSICIONES,)


def test_tope_propio_de_una_sola_entrada_de_mayor_riesgo():
    r = select_entries(
        [_c("BTCUSDT", "54", TierEntrada.MAYOR_RIESGO, 1), _c("ETHUSDT", "53", TierEntrada.MAYOR_RIESGO, 2)],
        _cupos(),
    )
    assert _simbolos(r) == ["BTCUSDT"]
    assert r.descartadas[0].motivos == (MotivoRechazoEntrada.MAXIMO_MAYOR_RIESGO,)


def test_una_posicion_de_mayor_riesgo_ya_abierta_bloquea_otra_pero_no_a_las_normales():
    r = select_entries(
        [_c("BTCUSDT", "54", TierEntrada.MAYOR_RIESGO, 1), _c("ETHUSDT", "60", TierEntrada.NORMAL, 2)],
        _cupos(mayor_riesgo_abiertas=1, posiciones_abiertas=1, simbolos_abiertos=frozenset({"SOLUSDT"})),
    )
    assert _simbolos(r) == ["ETHUSDT"]
    assert r.descartadas[0].candidato.symbol == "BTCUSDT"


def test_una_mayor_riesgo_pendiente_de_aprobacion_reserva_cupo_para_las_siguientes():
    # Reserva de cupos (decisión 2026-10-04): la mayor riesgo se selecciona
    # (pedirá aprobación) y ya consume 1 de las 3 entradas y 1 de las 4 posiciones.
    r = select_entries(
        [_c("BTCUSDT", "54", TierEntrada.MAYOR_RIESGO, 1)]
        + [_c(s, str(60 + i), n=10 + i) for i, s in enumerate(["ETHUSDT", "SOLUSDT", "ADAUSDT"])],
        _cupos(),
    )
    # 3 normales (prioridad) + 0 de mayor riesgo: las 3 entradas del día se agotan antes.
    assert _simbolos(r) == ["ADAUSDT", "SOLUSDT", "ETHUSDT"]
    assert r.descartadas[0].candidato.symbol == "BTCUSDT"


def test_con_cupo_la_de_mayor_riesgo_entra_y_exige_aprobacion():
    r = select_entries(
        [_c("BTCUSDT", "54", TierEntrada.MAYOR_RIESGO, 1), _c("ETHUSDT", "60", TierEntrada.NORMAL, 2)], _cupos()
    )
    por_simbolo = {s.candidato.symbol: s for s in r.seleccionadas}
    assert por_simbolo["ETHUSDT"].requiere_aprobacion_manual is False
    assert por_simbolo["BTCUSDT"].requiere_aprobacion_manual is True
    assert all(s.monto_usd == D(100) for s in r.seleccionadas)


def test_nunca_se_abre_dos_veces_el_mismo_simbolo_en_un_ciclo():
    r = select_entries(
        [_c("BTCUSDT", "70", TierEntrada.NORMAL, 1), _c("BTCUSDT", "60", TierEntrada.NORMAL, 2)], _cupos()
    )
    assert _simbolos(r) == ["BTCUSDT"]
    assert r.seleccionadas[0].candidato.puntaje_ponderado == D(70)
    assert r.descartadas[0].motivos == (MotivoRechazoEntrada.SIMBOLO_YA_ABIERTO,)


def test_un_simbolo_con_posicion_abierta_se_descarta():
    r = select_entries(
        [_c("BTCUSDT", "70", n=1)], _cupos(posiciones_abiertas=1, simbolos_abiertos=frozenset({"BTCUSDT"}))
    )
    assert r.seleccionadas == ()
    assert r.descartadas[0].motivos == (MotivoRechazoEntrada.SIMBOLO_YA_ABIERTO,)


def test_sistema_pausado_descarta_todo_con_su_motivo():
    r = select_entries([_c("BTCUSDT", "70", n=1), _c("ETHUSDT", "60", n=2)], _cupos(sistema_pausado=True))
    assert r.seleccionadas == ()
    assert all(MotivoRechazoEntrada.SISTEMA_PAUSADO in d.motivos for d in r.descartadas)
    assert len(r.descartadas) == 2


def test_tier_ninguno_y_simbolos_fuera_de_la_whitelist_se_descartan():
    r = select_entries(
        [_c("BTCUSDT", "40", TierEntrada.NINGUNO, 1), _c("FAKEUSDT", "90", TierEntrada.NORMAL, 2),
         _c("ETHUSDT", "60", TierEntrada.NORMAL, 3)],
        _cupos(),
    )
    assert _simbolos(r) == ["ETHUSDT"]
    motivos = {d.candidato.symbol: d.motivos for d in r.descartadas}
    assert MotivoRechazoEntrada.TIER_NO_CALIFICA in motivos["BTCUSDT"]
    assert MotivoRechazoEntrada.SIMBOLO_NO_PERMITIDO in motivos["FAKEUSDT"]


def test_un_descartado_no_consume_cupo():
    # FAKEUSDT (primero por puntaje) se descarta y NO debe gastar ninguna de las 3 entradas.
    r = select_entries(
        [_c("FAKEUSDT", "99", n=1), _c("BTCUSDT", "80", n=2), _c("ETHUSDT", "70", n=3), _c("SOLUSDT", "60", n=4)],
        _cupos(),
    )
    assert _simbolos(r) == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_el_empate_exacto_se_resuelve_por_simbolo_y_es_independiente_del_orden_de_entrada():
    candidatos = [_c("SOLUSDT", "65", n=1), _c("BTCUSDT", "65", n=2), _c("ETHUSDT", "65", n=3)]
    esperado = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    for permutacion in itertools.permutations(candidatos):
        assert _simbolos(select_entries(list(permutacion), _cupos())) == esperado


def test_la_seleccion_es_determinista_y_no_muta_la_entrada():
    candidatos = [_c("ETHUSDT", "60", n=1), _c("BTCUSDT", "70", n=2)]
    copia = list(candidatos)
    assert select_entries(candidatos, _cupos()) == select_entries(candidatos, _cupos())
    assert candidatos == copia


def test_el_resultado_conserva_orden_de_prioridad_en_descartadas():
    r = select_entries(
        [_c("BTCUSDT", "60", n=1), _c("ETHUSDT", "55", n=2), _c("FAKE1USDT", "90", n=3), _c("FAKE2USDT", "80", n=4)],
        _cupos(),
    )
    assert [d.candidato.symbol for d in r.descartadas] == ["FAKE1USDT", "FAKE2USDT"]
