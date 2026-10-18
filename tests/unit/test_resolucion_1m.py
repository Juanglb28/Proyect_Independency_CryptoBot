"""Resolución de velas ambiguas con velas de 1 minuto (estudio, 2026-10-17).

Casos calculados a mano. Sin resolvedor (o si no devuelve datos) NADA cambia: es la
lectura de todos los resultados publicados.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from app.data.exchange_client import Candle
from app.data.exit_simulation import (
    AMBIGUA_ORDEN_TRAILING,
    AMBIGUA_PARCIAL_Y_TRAILING,
    AMBIGUA_STOP_Y_OBJETIVO,
    clase_ambiguedad,
    retorno_marcado_a_mercado,
    simular_salida,
)
from app.domain.rules.risk import (
    COMISION_POR_LADO,
    TAKE_PROFIT_FRACCION_A_CERRAR,
    calculate_initial_levels,
    calculate_trailing_stop,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
UNO = Decimal(1)
ENTRADA = Decimal(100)
NIV = calculate_initial_levels(ENTRADA)  # stop 92, objetivo 112


def _v(i: int, o, h, low, c) -> Candle:
    return Candle(
        symbol="X", timeframe="1h", open_time=T0 + timedelta(hours=i),
        open=Decimal(str(o)), high=Decimal(str(h)), low=Decimal(str(low)), close=Decimal(str(c)),
        volume=Decimal(1),
    )


def _minutos(hora: int, plano, **minutos) -> list[Candle]:
    """60 velas de 1 minuto planas en `plano`; `m5=(alto, bajo)` fija el rango del minuto 5."""
    salida = []
    for k in range(60):
        alto, bajo = minutos.get(f"m{k}", (plano, plano))
        salida.append(Candle(
            symbol="X", timeframe="1m", open_time=T0 + timedelta(hours=hora, minutes=k),
            open=Decimal(str(plano)), high=Decimal(str(alto)), low=Decimal(str(bajo)), close=Decimal(str(plano)),
            volume=Decimal(1),
        ))
    return salida


def _retorno_con_trailing(pico: int) -> Decimal:
    mitad = (UNO / ENTRADA) * TAKE_PROFIT_FRACCION_A_CERRAR
    cobrado = mitad * NIV.target_price * (UNO - COMISION_POR_LADO)
    cobrado += mitad * calculate_trailing_stop(Decimal(pico)) * (UNO - COMISION_POR_LADO)
    return cobrado - UNO - COMISION_POR_LADO


def _resolvedor(por_hora: dict[int, list[Candle]]):
    """Resolvedor falso: entrega los minutos de la hora indicada, o None."""
    mock = MagicMock()
    mock.side_effect = lambda vela, clase: por_hora.get(int((vela["open_time"] - T0).total_seconds() // 3600))
    return mock


# -- clase_ambiguedad ---------------------------------------------------------


def test_clases_de_ambiguedad_antes_del_cierre_parcial():
    assert clase_ambiguedad(_v(1, 100, 113, 91, 100), niveles=NIV, pico=None) == AMBIGUA_STOP_Y_OBJETIVO
    assert clase_ambiguedad(_v(1, 100, 120, 104, 110), niveles=NIV, pico=None) == AMBIGUA_PARCIAL_Y_TRAILING
    # Toca el objetivo pero el mínimo queda por encima del trailing: ninguna lectura cambia nada.
    assert clase_ambiguedad(_v(1, 100, 113, 107, 112), niveles=NIV, pico=None) is None
    # Solo toca el stop, o ni uno ni otro: no hay ambigüedad.
    assert clase_ambiguedad(_v(1, 100, 105, 91, 95), niveles=NIV, pico=None) is None
    assert clase_ambiguedad(_v(1, 100, 105, 99, 101), niveles=NIV, pico=None) is None


def test_clase_de_ambiguedad_despues_del_cierre_parcial():
    pico = Decimal(112)  # trailing viejo 105,28
    assert clase_ambiguedad(_v(2, 112, 120, 105, 110), niveles=NIV, pico=pico) == AMBIGUA_ORDEN_TRAILING
    assert clase_ambiguedad(_v(2, 112, 120, 113, 118), niveles=NIV, pico=pico) is None  # mínimo sobre el trailing nuevo
    assert clase_ambiguedad(_v(2, 112, 112, 100, 101), niveles=NIV, pico=pico) is None  # no supera el pico: iguales


def test_la_clase_de_ambiguedad_coincide_con_que_las_dos_lecturas_difieran():
    # Condición necesaria y suficiente: ambigua <=> las dos lecturas dan resultados distintos.
    from app.data.exit_simulation import MAXIMO_PRIMERO, MINIMO_PRIMERO

    previas = [_v(0, 100, 100, 100, 100), _v(1, 100, 113, 107, 112)]  # parcial sin ambigüedad: pico 112
    for alto in (112, 113, 118, 125, 140):
        for bajo in (95, 104, 105, 106, 108, 112, 118):
            if bajo > alto:
                continue
            velas = [*previas, _v(2, 112, alto, bajo, 112)]
            a = simular_salida(velas, 0, orden_intravela=MAXIMO_PRIMERO)
            b = simular_salida(velas, 0, orden_intravela=MINIMO_PRIMERO)
            ambigua = clase_ambiguedad(velas[2], niveles=NIV, pico=Decimal(112)) is not None
            # Distinto desenlace (incl. «sale» vs «sigue abierta»)
            assert (a != b) == ambigua, (alto, bajo)


# -- el resolvedor en la simulación ---------------------------------------------


def test_sin_resolvedor_o_sin_datos_el_resultado_es_el_de_siempre():
    velas = [_v(0, 100, 100, 100, 100), _v(1, 100, 113, 107, 112), _v(2, 112, 120, 105, 110)]
    base = simular_salida(velas, 0)
    assert simular_salida(velas, 0, resolvedor=None) == base
    assert simular_salida(velas, 0, resolvedor=_resolvedor({})) == base  # devuelve None: regla de 1h
    assert retorno_marcado_a_mercado(velas, 0, resolvedor=_resolvedor({})) == retorno_marcado_a_mercado(velas, 0)


def test_si_nada_es_ambiguo_no_se_consulta_el_resolvedor():
    velas = [_v(0, 100, 100, 100, 100), _v(1, 100, 105, 99, 101), _v(2, 101, 106, 100, 104)]
    r = _resolvedor({})
    simular_salida(velas, 0, resolvedor=r)
    r.assert_not_called()


def _tras_parcial():
    return [_v(0, 100, 100, 100, 100), _v(1, 100, 113, 107, 112), _v(2, 112, 120, 105, 110)]


def test_orden_trailing_maximo_antes_del_minimo_coincide_con_la_lectura_publicada():
    minutos = _minutos(2, 110, m5=(120, 118), m6=(118, 105))
    s = simular_salida(_tras_parcial(), 0, resolvedor=_resolvedor({2: minutos}))
    assert s is not None and s.motivo == "TRAILING_STOP" and s.velas_hasta_cierre == 2
    assert s.retorno_neto == _retorno_con_trailing(120)  # trailing de 120 = 112,8
    assert s == simular_salida(_tras_parcial(), 0)


def test_orden_trailing_minimo_antes_del_maximo_coincide_con_la_lectura_inversa():
    minutos = _minutos(2, 110, m5=(111, 105), m6=(120, 119))
    s = simular_salida(_tras_parcial(), 0, resolvedor=_resolvedor({2: minutos}))
    assert s is not None and s.retorno_neto == _retorno_con_trailing(112)  # trailing viejo 105,28


def test_orden_trailing_un_camino_intermedio_da_un_valor_distinto_de_las_dos_lecturas():
    minutos = _minutos(2, 110, m5=(114, 113), m6=(113, 107))
    s = simular_salida(_tras_parcial(), 0, resolvedor=_resolvedor({2: minutos}))
    assert s is not None and s.retorno_neto == _retorno_con_trailing(114)  # 107,16
    from app.data.exit_simulation import MINIMO_PRIMERO

    assert _retorno_con_trailing(112) < s.retorno_neto < _retorno_con_trailing(120)
    assert s.retorno_neto != simular_salida(_tras_parcial(), 0).retorno_neto
    assert s.retorno_neto != simular_salida(_tras_parcial(), 0, orden_intravela=MINIMO_PRIMERO).retorno_neto


def test_stop_y_objetivo_en_la_misma_vela_con_el_stop_primero_es_stop():
    velas = [_v(0, 100, 100, 100, 100), _v(1, 100, 113, 91, 100)]
    minutos = _minutos(1, 100, m3=(100, 91), m4=(113, 112.5))
    s = simular_salida(velas, 0, resolvedor=_resolvedor({1: minutos}))
    assert s is not None and s.motivo == "STOP_LOSS"
    assert s == simular_salida(velas, 0)


def test_stop_y_objetivo_en_la_misma_vela_con_el_objetivo_primero_cierra_parcial_y_luego_trailing():
    velas = [_v(0, 100, 100, 100, 100), _v(1, 100, 113, 91, 100)]
    minutos = _minutos(1, 100, m3=(113, 112.5), m4=(100, 91))
    s = simular_salida(velas, 0, resolvedor=_resolvedor({1: minutos}))
    assert s is not None and s.motivo == "TRAILING_STOP" and s.velas_hasta_cierre == 1
    assert s.retorno_neto == _retorno_con_trailing(112)
    assert s.retorno_neto > simular_salida(velas, 0).retorno_neto  # la regla de 1h daba STOP: aquí sale mejor


def test_parcial_y_trailing_en_la_misma_vela_se_evalua_el_resto_de_la_hora():
    velas = [_v(0, 100, 100, 100, 100), _v(1, 100, 120, 104, 110), _v(2, 110, 111, 109, 110)]
    assert simular_salida(velas, 0) is None  # la regla de 1h no evalúa el resto: sigue abierta
    minutos = _minutos(1, 110, m2=(112.5, 112), m3=(120, 119), m4=(110, 104))
    s = simular_salida(velas, 0, resolvedor=_resolvedor({1: minutos}))
    assert s is not None and s.velas_hasta_cierre == 1 and s.retorno_neto == _retorno_con_trailing(120)


def test_el_estado_se_conserva_al_pasar_de_una_vela_de_1h_a_sus_minutos_y_de_vuelta():
    # La vela ambigua deja la posición abierta con un pico nuevo; la siguiente vela de 1h lo usa.
    velas = [*_tras_parcial()[:2], _v(2, 112, 120, 105, 110), _v(3, 110, 112, 100, 101)]
    minutos = _minutos(2, 116, m5=(120, 119))  # el mínimo nunca toca el trailing (112,8): sigue abierta, pico 120
    s = simular_salida(velas, 0, resolvedor=_resolvedor({2: minutos}))
    assert s is not None and s.velas_hasta_cierre == 3  # sale en la vela siguiente, con el pico de 120
    assert s.retorno_neto == _retorno_con_trailing(120)
