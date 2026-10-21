"""Construcción de observaciones del backtest (app.data.backtest_study)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.data.backtest_study import observaciones_de_simbolo
from app.data.exchange_client import Candle
from app.data.exit_simulation import simular_salida
from app.domain.rules.quantitative_score import QUANTITATIVE_FORMULA_VERSION

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
DIAS = 260  # > 180 de SMA: hay días con score


def _precio(h: float) -> Decimal:
    """Serie suave con tendencia y ciclos (determinista)."""
    return Decimal(str(round(100 + h * 0.02 + 8 * math.sin(h / 60) + 3 * math.sin(h * 1.7), 6)))


def _diarias(n: int = DIAS) -> list[Candle]:
    velas = []
    for d in range(n):
        c = _precio(d * 24 + 23)
        o = _precio(d * 24)
        velas.append(
            Candle(symbol="X", timeframe="1d", open_time=T0 + timedelta(days=d), open=o,
                   high=max(o, c) * Decimal("1.01"), low=min(o, c) * Decimal("0.99"), close=c,
                   volume=Decimal(1000 + (d * 37) % 400))
        )
    return velas


def _horarias(n_dias: int = DIAS) -> list[Candle]:
    velas = []
    for h in range(n_dias * 24):
        o, c = _precio(h), _precio(h + 1)
        velas.append(
            Candle(symbol="X", timeframe="1h", open_time=T0 + timedelta(hours=h), open=o,
                   high=max(o, c) * Decimal("1.001"), low=min(o, c) * Decimal("0.999"), close=c, volume=Decimal(10))
        )
    return velas


def test_genera_observaciones_solo_donde_el_score_es_calculable():
    obs = observaciones_de_simbolo("X", _diarias(), _horarias())
    assert obs, "debe haber observaciones después de la calentada"
    primera = min(o.dia for o in obs)
    # SMA_180 necesita 180 velas diarias: la primera decisión es el día 181 (índice 179 + 1).
    assert primera == (T0 + timedelta(days=180)).date()
    assert all(o.symbol == "X" and Decimal(0) <= o.score <= Decimal(100) for o in obs)


def test_sin_look_ahead_el_score_del_dia_no_cambia_si_se_recorta_el_futuro():
    """El score de la decisión del día D debe ser idéntico calculado con toda la
    serie o solo con las velas hasta D-1: ningún dato posterior lo toca."""
    diarias, horarias = _diarias(), _horarias()
    completas = {o.dia: o for o in observaciones_de_simbolo("X", diarias, horarias)}
    for corte in (200, 230, 250):  # nº de velas diarias conocidas
        # Se recorta SOLO la serie diaria (de donde sale el score); las de 1h quedan completas
        # para que la última decisión recortada siga teniendo precio de entrada.
        recortadas = observaciones_de_simbolo("X", diarias[:corte], horarias)
        ultimo = max(o.dia for o in recortadas)
        assert ultimo == (T0 + timedelta(days=corte)).date()  # la decisión del último día recortado SÍ se evalúa
        for o in recortadas:
            assert o.score == completas[o.dia].score, f"el score del {o.dia} cambió con datos futuros"


def test_el_desenlace_viene_de_las_reglas_del_motor_paper_desde_el_precio_de_las_0000():
    diarias, horarias = _diarias(), _horarias()
    obs = {o.dia: o for o in observaciones_de_simbolo("X", diarias, horarias)}
    dia = (T0 + timedelta(days=200)).date()
    decision = datetime(dia.year, dia.month, dia.day, tzinfo=timezone.utc)
    indice = next(i for i, c in enumerate(horarias) if c["open_time"] == decision - timedelta(hours=1))
    esperado = simular_salida(horarias, indice)
    o = obs[dia]
    if esperado is None:
        assert o.retorno_neto is None and o.cierre_en is None
    else:
        assert o.retorno_neto == esperado.retorno_neto
        assert o.cierre_en == decision + timedelta(hours=esperado.velas_hasta_cierre)


def test_sin_datos_horarios_posteriores_la_observacion_queda_censurada_no_rellenada():
    diarias, horarias = _diarias(), _horarias()
    obs = observaciones_de_simbolo("X", diarias, horarias[: 255 * 24])  # velas 1h se acaban el día 255
    tardias = [o for o in obs if o.dia >= (T0 + timedelta(days=254)).date()]
    assert tardias
    # Las observaciones sin velas de 1h que las cubran no existen; las que quedan al borde pueden ser censuradas.
    assert all((o.retorno_neto is None) == (o.cierre_en is None) for o in obs)
    assert max(o.dia for o in obs) <= (T0 + timedelta(days=255)).date()


def test_listas_vacias_no_generan_nada():
    assert observaciones_de_simbolo("X", [], _horarias()) == []
    assert observaciones_de_simbolo("X", _diarias(), []) == []


def test_la_version_de_la_formula_es_la_de_produccion():
    # Si cambia la fórmula en producción, los resultados históricos del backtest dejan de
    # corresponder: este test obliga a revisar el backtest al subir la versión.
    assert QUANTITATIVE_FORMULA_VERSION == "1"


def test_cada_observacion_lleva_sus_subscores_y_el_retorno_marcado():
    obs = observaciones_de_simbolo("X", _diarias(), _horarias())
    for o in obs:
        nombres = [n for n, _ in o.factores]
        assert nombres == ["tendencia", "volumen", "distancia_max_90d", "rsi"]
        promedio = sum((v for _, v in o.factores), Decimal(0)) / 4
        assert promedio == o.score  # el score es el promedio de los 4 sub-scores (pesos iguales)
        assert o.retorno_marcado is not None and o.marcado_en is not None
        if o.retorno_neto is not None:
            assert o.retorno_marcado == o.retorno_neto  # cerrada: marcado = resultado real


# --------------------------------------- fricción de ejecución (prueba de estrés, 2026-10-21)
def test_sin_friccion_explicita_las_observaciones_son_identicas_a_las_publicadas():
    from app.data.exit_simulation import SIN_FRICCION

    diarias, horarias = _diarias(), _horarias()
    assert observaciones_de_simbolo("X", diarias, horarias, friccion=SIN_FRICCION) == observaciones_de_simbolo(
        "X", diarias, horarias
    )


def test_la_friccion_no_cambia_el_score_ni_los_dias_solo_el_desenlace():
    from app.data.exit_simulation import Friccion

    diarias, horarias = _diarias(), _horarias()
    base = observaciones_de_simbolo("X", diarias, horarias)
    con = observaciones_de_simbolo("X", diarias, horarias, friccion=Friccion(slippage_por_lado=Decimal("0.005"), huecos=True))
    assert [(o.symbol, o.dia, o.score) for o in con] == [(o.symbol, o.dia, o.score) for o in base]


def test_la_friccion_se_pasa_a_la_simulacion_de_salida():
    from app.data.exit_simulation import Friccion

    diarias, horarias = _diarias(), _horarias()
    friccion = Friccion(slippage_por_lado=Decimal("0.01"))
    dia = (T0 + timedelta(days=200)).date()
    decision = datetime(dia.year, dia.month, dia.day, tzinfo=timezone.utc)
    indice = next(i for i, c in enumerate(horarias) if c["open_time"] == decision - timedelta(hours=1))
    esperado = simular_salida(horarias, indice, friccion=friccion)
    o = {x.dia: x for x in observaciones_de_simbolo("X", diarias, horarias, friccion=friccion)}[dia]
    if esperado is None:
        assert o.retorno_neto is None
    else:
        assert o.retorno_neto == esperado.retorno_neto
