"""Reglas puras de escalamiento (`app.domain.rules.escalation`): umbrales
exactos, referencia de "hace una hora", z-score de volumen, estado de Binance
y textos de los avisos."""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.enums import TipoAlerta
from app.domain.rules import escalation as e
from app.domain.rules.risk import MotivoRechazoEntrada, SituacionDeCupos, TierEntrada, validate_entry

D = Decimal
NOW = datetime(2026, 10, 7, 16, 30, tzinfo=timezone.utc)
HACE_1H = NOW - timedelta(hours=1)


# --- caída en 1 hora ---


def _caida(precio_actual, precio_ref="100", hora_ref=HACE_1H):
    return e.evaluar_caida_1h(
        precio_referencia=D(precio_ref), hora_referencia=hora_ref, precio_actual=D(precio_actual), now=NOW
    )


def test_exactamente_5_por_ciento_no_avisa_y_un_poco_mas_si():
    assert _caida("95") is None
    assert _caida("94.99") is not None


def test_la_caida_se_reporta_como_porcentaje_positivo():
    caida = _caida("93.5")
    assert caida.caida_pct == D("6.5") and caida.precio_referencia == D("100")


def test_una_subida_o_una_caida_menor_no_avisan():
    assert _caida("101") is None and _caida("97") is None


@pytest.mark.parametrize(
    "antiguedad, valida",
    [
        (timedelta(minutes=59), False),  # todavía no tiene una hora
        (timedelta(hours=1), True),
        (timedelta(hours=1, minutes=5), True),  # justo en la tolerancia
        (timedelta(hours=1, minutes=5, seconds=1), False),  # hueco de datos
    ],
)
def test_la_referencia_debe_ser_de_hace_una_hora_con_tolerancia(antiguedad, valida):
    assert e.referencia_de_caida_valida(NOW - antiguedad, now=NOW) is valida


def test_con_referencia_no_valida_no_se_avisa_aunque_el_precio_se_desplome():
    assert _caida("50", hora_ref=NOW - timedelta(minutes=30)) is None
    assert _caida("50", hora_ref=NOW - timedelta(hours=3)) is None


def test_precios_no_positivos_se_rechazan():
    with pytest.raises(ValueError):
        _caida("0")
    with pytest.raises(ValueError):
        _caida("90", precio_ref="-1")


def test_los_instantes_sin_zona_horaria_se_rechazan():
    with pytest.raises(ValueError):
        e.evaluar_caida_1h(
            precio_referencia=D(100), hora_referencia=HACE_1H, precio_actual=D(90), now=datetime(2026, 10, 7, 16, 30)
        )


# --- volumen atípico ---


def test_ultima_vela_cerrada_es_la_de_la_hora_anterior():
    assert e.ultima_vela_1h_cerrada(datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)) == datetime(
        2026, 10, 7, 14, 0, tzinfo=timezone.utc
    )
    assert e.ultima_vela_1h_cerrada(datetime(2026, 10, 7, 15, 59, 59, tzinfo=timezone.utc)) == datetime(
        2026, 10, 7, 14, 0, tzinfo=timezone.utc
    )
    with pytest.raises(ValueError):
        e.ultima_vela_1h_cerrada(datetime(2026, 10, 7, 15, 0))


def test_las_30_horas_de_referencia_son_consecutivas_y_terminan_antes_de_la_vela():
    objetivo = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)
    horas = e.horas_de_referencia_volumen(objetivo)
    assert len(horas) == 30 and horas[-1] == objetivo - timedelta(hours=1)
    assert all(b - a == timedelta(hours=1) for a, b in zip(horas, horas[1:]))


def test_zscore_coincide_con_la_desviacion_muestral():
    previos = [D(10 + (i % 5)) for i in range(30)]
    z = e.calcular_zscore_volumen(D(40), previos)
    esperado = (40 - statistics.mean(map(float, previos))) / statistics.stdev(map(float, previos))
    assert float(z) == pytest.approx(esperado, abs=1e-6)


def test_zscore_sin_30_previos_o_sin_variacion_no_existe():
    assert e.calcular_zscore_volumen(D(5), [D(1)] * 29) is None
    assert e.calcular_zscore_volumen(D(5), [D(1)] * 31) is None
    assert e.calcular_zscore_volumen(D(5), [D(1)] * 30) is None  # desviación 0


def test_volumen_atipico_es_estrictamente_mayor_a_3():
    assert e.es_volumen_atipico(D("3.000001")) is True
    assert e.es_volumen_atipico(D("3")) is False
    assert e.es_volumen_atipico(D("-9")) is False  # un volumen bajo no es el aviso pedido
    assert e.es_volumen_atipico(None) is False


# --- Binance ---

PREV = NOW - timedelta(minutes=6)


def _binance(ultimo=None, consultadas=1, obtenidos=0, ultima=PREV, now=NOW):
    return e.decidir_aviso_binance(
        ultimo_aviso=ultimo,
        posiciones_consultadas=consultadas,
        precios_obtenidos=obtenidos,
        ultima_respuesta=ultima,
        now=now,
    )


def test_cae_solo_si_pasan_mas_de_5_minutos_sin_precio():
    assert _binance(ultima=NOW - timedelta(minutes=5)) is None  # exactamente 5 min
    assert _binance(ultima=NOW - timedelta(minutes=5, seconds=1)) == TipoAlerta.BINANCE_CAIDO


def test_un_precio_parcial_no_es_una_caida():
    assert _binance(consultadas=2, obtenidos=1) is None


def test_no_repite_el_aviso_si_ya_figura_caido():
    assert _binance(ultimo=TipoAlerta.BINANCE_CAIDO) is None


def test_se_restablece_cuando_llega_un_precio_y_figuraba_caido():
    assert _binance(ultimo=TipoAlerta.BINANCE_CAIDO, obtenidos=1) == TipoAlerta.BINANCE_RESTABLECIDO
    assert _binance(ultimo=TipoAlerta.BINANCE_RESTABLECIDO, obtenidos=1) is None
    assert _binance(ultimo=None, obtenidos=1) is None


def test_sin_posiciones_o_sin_ultima_respuesta_no_se_declara_nada():
    assert _binance(consultadas=0, ultimo=TipoAlerta.BINANCE_CAIDO) is None
    assert _binance(ultima=None) is None


def test_binance_figura_caido_solo_con_ultimo_aviso_caido():
    assert e.binance_figura_caido(TipoAlerta.BINANCE_CAIDO) is True
    assert e.binance_figura_caido(TipoAlerta.BINANCE_RESTABLECIDO) is False
    assert e.binance_figura_caido(None) is False


def test_binance_caido_bloquea_la_entrada_nueva():
    cupos = SituacionDeCupos(0, 0, 0, frozenset(), sistema_pausado=False, binance_caido=True)
    v = validate_entry(symbol="BTCUSDT", tier=TierEntrada.NORMAL, cupos=cupos)
    assert v.permitida is False and v.motivos_rechazo == (MotivoRechazoEntrada.BINANCE_CAIDO,)
    libre = SituacionDeCupos(0, 0, 0, frozenset(), sistema_pausado=False)
    assert validate_entry(symbol="BTCUSDT", tier=TierEntrada.NORMAL, cupos=libre).permitida is True


# --- textos ---


def test_los_mensajes_son_texto_corto_con_el_simbolo_y_las_cifras():
    caida = _caida("93.5")
    assert "ETHUSDT" in e.mensaje_caida_1h("ETHUSDT", caida) and "6.50" in e.mensaje_caida_1h("ETHUSDT", caida)
    msg = e.mensaje_volumen_atipico("ETHUSDT", vela=HACE_1H, zscore=D("4.2"), volumen=D("1500"))
    assert "ETHUSDT" in msg and "4.20" in msg
    assert "100.00 USD" in e.mensaje_orden_pendiente("SOLUSDT", monto_usd=D(100), vence=NOW)
    assert "-15 %" in e.mensaje_pausa_drawdown(equity=D(850), peak=D(1000), drawdown_pct=D("-15"))
    assert "no se abrirán entradas nuevas" in e.mensaje_binance_caido(ultima_respuesta=PREV, now=NOW)
    assert "RESTABLECIDO" in e.mensaje_binance_restablecido(now=NOW)


def test_el_mensaje_de_discrepancia_recorta_la_lista():
    msg = e.mensaje_discrepancia([f"d{i}" for i in range(8)])
    assert "8 hallazgo" in msg and "d4" in msg and "d5" not in msg and "y 3 más" in msg
    assert len(msg) < 4096
