"""Resolvedor de velas de 1 minuto: validación contra la vela de 1h, caché y fallos de red."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import ccxt
import pytest

from app.data.exchange_client import Candle, fetch_minute_candles_of_hour
from app.data.exit_simulation import AMBIGUA_ORDEN_TRAILING
from app.data.minute_candles import (
    MOTIVO_ERROR_DE_RED,
    MOTIVO_INCOMPLETA,
    MOTIVO_INCONSISTENTE,
    MOTIVO_SIN_DESCARGAR,
    ResolvedorMinutos,
    validar_minutos,
)

HORA = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
SYM = "BTCUSDT"


def _raw(n=60, hora=HORA):
    """Filas ccxt [ms, o, h, l, c, v]: precio plano 100; minuto 7 llega a 110 y minuto 9 baja a 90."""
    filas = []
    for k in range(n):
        ms = int((hora + timedelta(minutes=k)).timestamp() * 1000)
        alto, bajo = (110.0, 100.0) if k == 7 else (100.0, 90.0) if k == 9 else (100.0, 100.0)
        filas.append([ms, 100.0, alto, bajo, 100.0, 1.0])
    return filas


def _vela(alto="110", bajo="90", abre="100", cierra="100", hora=HORA) -> Candle:
    return Candle(symbol=SYM, timeframe="1h", open_time=hora, open=Decimal(abre), high=Decimal(alto),
                  low=Decimal(bajo), close=Decimal(cierra), volume=Decimal(60))


class _Exchange:
    def __init__(self, filas=None, error=None):
        self.filas, self.error, self.llamadas = filas, error, []

    def fetch_ohlcv(self, simbolo, timeframe, limit, since):
        self.llamadas.append((simbolo, timeframe, limit, since))
        if self.error is not None:
            raise self.error
        return self.filas


def _minutos(exchange=None):
    return fetch_minute_candles_of_hour(SYM, HORA, exchange or _Exchange(_raw()))


# -- descarga ----------------------------------------------------------------


def test_la_descarga_pide_60_velas_de_1m_desde_el_inicio_de_la_hora():
    ex = _Exchange(_raw())
    velas = fetch_minute_candles_of_hour(SYM, HORA, ex)
    assert ex.llamadas == [("BTC/USDT", "1m", 60, int(HORA.timestamp() * 1000))]
    assert len(velas) == 60 and velas[0]["open_time"] == HORA and velas[0]["timeframe"] == "1m"


@pytest.mark.parametrize("hora", [HORA.replace(minute=30), HORA.replace(tzinfo=None), HORA.replace(second=1)])
def test_la_descarga_exige_el_inicio_exacto_de_una_hora_con_zona_horaria(hora):
    with pytest.raises(ValueError):
        fetch_minute_candles_of_hour(SYM, hora, _Exchange(_raw()))


def test_la_descarga_rechaza_simbolos_fuera_de_la_lista_autorizada():
    with pytest.raises(ValueError):
        fetch_minute_candles_of_hour("FAKEUSDT", HORA, _Exchange(_raw()))


# -- validación ---------------------------------------------------------------


def test_minutos_que_agregan_exactamente_la_vela_son_validos():
    assert validar_minutos(_vela(), _minutos()) is None


def test_faltan_minutos():
    assert validar_minutos(_vela(), _minutos(_Exchange(_raw(59)))) == MOTIVO_INCOMPLETA


def test_minutos_no_consecutivos():
    filas = _raw()
    del filas[10]
    filas.append(filas[-1][:])  # 60 filas pero con un hueco y un duplicado
    assert validar_minutos(_vela(), _minutos(_Exchange(filas))) == MOTIVO_INCOMPLETA


@pytest.mark.parametrize("cambio", [dict(alto="111"), dict(bajo="89"), dict(abre="99"), dict(cierra="101")])
def test_si_el_agregado_no_coincide_con_la_vela_de_1h_no_se_usa(cambio):
    assert validar_minutos(_vela(**cambio), _minutos()) == MOTIVO_INCONSISTENTE


# -- el resolvedor ---------------------------------------------------------------


def test_descarga_valida_y_guarda_en_cache(tmp_path):
    ex = _Exchange(_raw())
    r = ResolvedorMinutos(directorio=tmp_path, exchange=ex)
    minutos = r(_vela(), AMBIGUA_ORDEN_TRAILING)
    assert minutos is not None and len(minutos) == 60
    assert r.descargadas == 1 and r.desde_cache == 0 and not r.no_resueltas
    assert r.max_rango_minuto == Decimal(10) / Decimal(90)  # minuto 9: (100-90)/90, el mayor
    r.guardar()
    guardado = json.loads((tmp_path / f"{SYM}.json").read_text())
    assert list(guardado) == [HORA.isoformat()] and len(guardado[HORA.isoformat()]) == 60
    assert not list(tmp_path.glob("*.tmp"))


def test_la_segunda_corrida_usa_la_cache_y_no_toca_la_red(tmp_path):
    r1 = ResolvedorMinutos(directorio=tmp_path, exchange=_Exchange(_raw()))
    r1(_vela(), AMBIGUA_ORDEN_TRAILING)
    r1.guardar()
    sin_red = _Exchange(error=AssertionError("no debía llamar a Binance"))
    r2 = ResolvedorMinutos(directorio=tmp_path, exchange=sin_red)
    assert r2(_vela(), AMBIGUA_ORDEN_TRAILING) is not None
    assert r2.desde_cache == 1 and r2.descargadas == 0 and sin_red.llamadas == []


def test_modo_solo_contar_no_descarga_y_declara_pendiente(tmp_path):
    ex = _Exchange(_raw())
    r = ResolvedorMinutos(directorio=tmp_path, descargar=False, exchange=ex)
    assert r(_vela(), AMBIGUA_ORDEN_TRAILING) is None
    assert ex.llamadas == [] and r.motivos_no_resueltas() == {MOTIVO_SIN_DESCARGAR: 1}
    assert r.velas_ambiguas_unicas() == {(SYM, HORA)} and r.por_clase() == {AMBIGUA_ORDEN_TRAILING: 1}


def test_un_error_de_red_no_se_guarda_y_la_vela_queda_sin_resolver(tmp_path):
    r = ResolvedorMinutos(directorio=tmp_path, exchange=_Exchange(error=ccxt.NetworkError("caído")), pausa_reintento=0)
    assert r(_vela(), AMBIGUA_ORDEN_TRAILING) is None
    assert r.motivos_no_resueltas() == {MOTIVO_ERROR_DE_RED: 1}
    r.guardar()
    assert not (tmp_path / f"{SYM}.json").exists()


def test_la_red_reintenta_antes_de_rendirse(tmp_path):
    ex = _Exchange(error=ccxt.NetworkError("caído"))
    r = ResolvedorMinutos(directorio=tmp_path, exchange=ex, pausa_reintento=0)
    r(_vela(), AMBIGUA_ORDEN_TRAILING)
    assert len(ex.llamadas) == 3


def test_datos_inconsistentes_se_descartan_y_se_cuentan(tmp_path):
    r = ResolvedorMinutos(directorio=tmp_path, exchange=_Exchange(_raw()))
    assert r(_vela(alto="999"), AMBIGUA_ORDEN_TRAILING) is None
    assert r.motivos_no_resueltas() == {MOTIVO_INCONSISTENTE: 1}


def test_una_cache_corrupta_se_ignora_y_se_vuelve_a_descargar(tmp_path):
    (tmp_path / f"{SYM}.json").write_text("{no es json")
    r = ResolvedorMinutos(directorio=tmp_path, exchange=_Exchange(_raw()))
    assert r(_vela(), AMBIGUA_ORDEN_TRAILING) is not None and r.descargadas == 1


# -- minutos que podrían ser ambiguos por sí solos (diagnóstico del orden DENTRO del minuto) ---------


def test_un_minuto_de_rango_pequeno_nunca_es_ambiguo_y_uno_de_6_por_ciento_o_mas_si_puede_serlo():
    from app.data.minute_candles import minuto_puede_ser_ambiguo

    def m(alto, bajo):
        return Candle(symbol=SYM, timeframe="1m", open_time=HORA, open=Decimal(alto), high=Decimal(alto),
                      low=Decimal(bajo), close=Decimal(bajo), volume=Decimal(1))

    assert not minuto_puede_ser_ambiguo(m("100", "94.01"))
    assert minuto_puede_ser_ambiguo(m("100", "94"))  # el mínimo toca exactamente el trailing de su máximo
    assert minuto_puede_ser_ambiguo(m("100", "60"))


def test_el_resolvedor_registra_los_minutos_de_rango_suficiente(tmp_path):
    # En _raw: el minuto 7 va de 100 a 110 (9 % del máximo) y el minuto 9 de 100 a 90 (10 %); el resto es plano.
    r = ResolvedorMinutos(directorio=tmp_path, exchange=_Exchange(_raw()))
    assert r(_vela(), AMBIGUA_ORDEN_TRAILING) is not None
    assert sorted(r.minutos_ambiguos) == [(SYM, HORA + timedelta(minutes=7)), (SYM, HORA + timedelta(minutes=9))]
    assert r.max_rango_minuto == Decimal(10) / Decimal(90)


def test_desde_cache_cuenta_horas_distintas_leidas_del_archivo_no_consultas_repetidas(tmp_path):
    r1 = ResolvedorMinutos(directorio=tmp_path, exchange=_Exchange(_raw()))
    r1(_vela(), AMBIGUA_ORDEN_TRAILING)
    r1(_vela(), AMBIGUA_ORDEN_TRAILING)  # repetida en la misma corrida: no cuenta como «desde caché»
    assert (r1.descargadas, r1.desde_cache) == (1, 0)
    r1.guardar()
    r2 = ResolvedorMinutos(directorio=tmp_path, exchange=_Exchange(error=AssertionError("sin red")))
    r2(_vela(), AMBIGUA_ORDEN_TRAILING)
    r2(_vela(), AMBIGUA_ORDEN_TRAILING)
    assert (r2.descargadas, r2.desde_cache) == (0, 1)
