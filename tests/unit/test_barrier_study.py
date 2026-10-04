"""Tests del estudio descriptivo de barreras (`app/data/barrier_study.py`) y
del parseo de `scripts/study_stop_target.py`. Todo con velas sintéticas: sin
base de datos ni red."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.data.barrier_study import (
    ParBarreras,
    ResultadoBarrera,
    break_even_target_rate,
    evaluate_entry,
    evaluate_symbol,
    summarize,
    velas_a_dias,
)
from app.data.exchange_client import Candle
from scripts.study_stop_target import parse_barreras

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_PAR = ParBarreras(stop_pct=Decimal("0.06"), target_pct=Decimal("0.08"))


def _vela(i: int, *, high: str, low: str, close: str | None = None) -> Candle:
    c = Decimal(close if close is not None else low)
    return Candle(
        symbol="BTCUSDT",
        timeframe="1h",
        open_time=_T0 + timedelta(hours=i),
        open=c,
        high=Decimal(high),
        low=Decimal(low),
        close=c,
        volume=Decimal("1"),
    )


def _entrada(close: str = "100") -> Candle:
    return _vela(0, high=close, low=close, close=close)


def test_toca_objetivo_primero():
    velas = [_entrada(), _vela(1, high="104", low="99"), _vela(2, high="108", low="103")]
    r = evaluate_entry(velas, 0, _PAR)
    assert (r.resultado, r.velas_hasta_resolver) == (ResultadoBarrera.TARGET_PRIMERO, 2)


def test_toca_stop_primero():
    velas = [_entrada(), _vela(1, high="101", low="94"), _vela(2, high="120", low="100")]
    r = evaluate_entry(velas, 0, _PAR)
    assert (r.resultado, r.velas_hasta_resolver) == (ResultadoBarrera.STOP_PRIMERO, 1)


def test_igualdad_exacta_cuenta_como_toque_igual_que_risk_py():
    # stop = 94 (low <= 94), objetivo = 108 (high >= 108)
    assert evaluate_entry([_entrada(), _vela(1, high="100", low="94")], 0, _PAR).resultado == (
        ResultadoBarrera.STOP_PRIMERO
    )
    assert evaluate_entry([_entrada(), _vela(1, high="108", low="100")], 0, _PAR).resultado == (
        ResultadoBarrera.TARGET_PRIMERO
    )


def test_un_paso_antes_del_nivel_no_cuenta():
    velas = [_entrada(), _vela(1, high="107.999999999999", low="94.000000000001")]
    assert evaluate_entry(velas, 0, _PAR).resultado == ResultadoBarrera.SIN_RESOLVER


def test_misma_vela_con_ambos_niveles_es_ambigua_y_no_se_reparte():
    velas = [_entrada(), _vela(1, high="110", low="90")]
    r = evaluate_entry(velas, 0, _PAR)
    assert (r.resultado, r.velas_hasta_resolver) == (ResultadoBarrera.AMBIGUA, 1)


def test_sin_toque_antes_de_acabar_los_datos_queda_sin_resolver():
    velas = [_entrada(), _vela(1, high="101", low="99"), _vela(2, high="102", low="98")]
    r = evaluate_entry(velas, 0, _PAR)
    assert (r.resultado, r.velas_hasta_resolver) == (ResultadoBarrera.SIN_RESOLVER, None)


def test_no_usa_velas_anteriores_a_la_entrada_ni_la_propia_vela():
    # Antes de la entrada hay una vela que "tocaría" el stop; la propia vela de
    # entrada tiene un low muy bajo. Ninguna debe contar (sin look-ahead ni
    # contaminación): solo velas POSTERIORES al cierre de entrada.
    velas = [
        _vela(0, high="100", low="50", close="100"),  # anterior a la entrada
        _vela(1, high="101", low="10", close="100"),  # vela de entrada (su low no cuenta)
        _vela(2, high="109", low="99"),
    ]
    r = evaluate_entry(velas, 1, _PAR)
    assert (r.resultado, r.velas_hasta_resolver) == (ResultadoBarrera.TARGET_PRIMERO, 1)


def test_el_nivel_se_calcula_sobre_el_cierre_de_la_vela_de_entrada():
    velas = [_entrada("200"), _vela(1, high="215.99", low="190"), _vela(2, high="216", low="190")]
    # objetivo = 200 × 1.08 = 216; stop = 200 × 0.94 = 188 (no se toca: low 190)
    r = evaluate_entry(velas, 0, _PAR)
    assert (r.resultado, r.velas_hasta_resolver) == (ResultadoBarrera.TARGET_PRIMERO, 2)


def test_evaluate_symbol_respeta_el_paso_y_no_entra_en_la_ultima_vela():
    velas = [_vela(i, high="100", low="100") for i in range(10)]
    ev = evaluate_symbol(velas, [_PAR], paso_velas=3)
    assert len(ev[_PAR]) == 3  # índices 0, 3, 6; el 9 es la última vela y no tiene velas posteriores
    ev2 = evaluate_symbol(velas, [_PAR], paso_velas=1)
    assert len(ev2[_PAR]) == 9


def test_evaluate_symbol_paso_invalido():
    with pytest.raises(ValueError):
        evaluate_symbol([_entrada(), _vela(1, high="100", low="100")], [_PAR], paso_velas=0)


def test_summarize_cuenta_y_calcula_tasa_y_mediana():
    velas = [
        _entrada(),
        _vela(1, high="109", low="99", close="100"),  # entrada 0: objetivo en 1 vela
        _vela(2, high="100", low="93", close="100"),  # entrada 1 (close 100): stop en 1 vela
        _vela(3, high="100", low="100", close="100"),  # entrada 2: sin resolver
    ]
    ev = evaluate_symbol(velas, [_PAR], paso_velas=1)[_PAR]
    resumen = summarize(_PAR, ev)

    assert resumen.entradas == 3
    assert (resumen.target_primero, resumen.stop_primero, resumen.ambiguas, resumen.sin_resolver) == (1, 1, 0, 1)
    assert resumen.resueltas == 2
    assert resumen.tasa_target == Decimal("0.5")
    assert resumen.mediana_velas_hasta_resolver == 1.0


def test_summarize_sin_resueltas_no_inventa_tasa():
    resumen = summarize(_PAR, [])
    assert resumen.tasa_target is None
    assert resumen.mediana_velas_hasta_resolver is None


def test_break_even_con_comision_de_ida_y_vuelta():
    # p = (stop + 2c) / (objetivo + stop), c = 0.1% por lado
    plan = ParBarreras(Decimal("0.08"), Decimal("0.12"))
    propuesta = ParBarreras(Decimal("0.06"), Decimal("0.08"))
    assert break_even_target_rate(plan) == Decimal("0.082") / Decimal("0.20")
    assert break_even_target_rate(propuesta) == Decimal("0.062") / Decimal("0.14")
    assert break_even_target_rate(plan, comision_por_lado=Decimal(0)) == Decimal("0.4")


def test_par_de_barreras_invalido():
    with pytest.raises(ValueError):
        ParBarreras(Decimal("6"), Decimal("0.08"))  # 6 en vez de 0.06
    with pytest.raises(ValueError):
        ParBarreras(Decimal("0.06"), Decimal(0))


def test_velas_a_dias():
    assert velas_a_dias(24, "1h") == 1.0
    assert velas_a_dias(6, "4h") == 1.0
    with pytest.raises(ValueError):
        velas_a_dias(1, "5m")


def test_parse_barreras_convierte_puntos_a_fracciones():
    pares = parse_barreras("6:8, 8:12")
    assert pares == [
        ParBarreras(Decimal("0.06"), Decimal("0.08")),
        ParBarreras(Decimal("0.08"), Decimal("0.12")),
    ]


@pytest.mark.parametrize("texto", ["6", "6:", ":8", "a:b"])
def test_parse_barreras_rechaza_formato_invalido(texto):
    with pytest.raises(Exception):
        parse_barreras(texto)
