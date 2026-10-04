"""Tests de app.data.relative_strength.calculate_ratio_rsi."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.data.exchange_client import Candle
from app.data.relative_strength import calculate_ratio_rsi

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _serie(symbol: str, cierres: list[str], *, desde: int = 0) -> list[Candle]:
    return [
        Candle(
            symbol=symbol,
            timeframe="1d",
            open_time=_T0 + timedelta(days=desde + i),
            open=Decimal(c),
            high=Decimal(c),
            low=Decimal(c),
            close=Decimal(c),
            volume=Decimal("1"),
        )
        for i, c in enumerate(cierres)
    ]


def test_ratio_que_sube_siempre_da_rsi_100():
    simbolo = _serie("LTCUSDT", [str(100 + i) for i in range(20)])
    btc = _serie("BTCUSDT", ["100"] * 20)
    assert calculate_ratio_rsi(simbolo, btc) == Decimal(100)


def test_ratio_que_baja_siempre_da_rsi_0():
    simbolo = _serie("LTCUSDT", [str(200 - i) for i in range(20)])
    btc = _serie("BTCUSDT", ["100"] * 20)
    assert calculate_ratio_rsi(simbolo, btc) == Decimal(0)


def test_ratio_constante_da_rsi_neutro_50():
    simbolo = _serie("LTCUSDT", ["100"] * 20)
    btc = _serie("BTCUSDT", ["100"] * 20)
    assert calculate_ratio_rsi(simbolo, btc) == Decimal(50)


def test_el_ratio_descuenta_el_movimiento_de_btc():
    # LTC sube 1 por día pero BTC sube proporcionalmente más: el ratio baja.
    simbolo = _serie("LTCUSDT", [str(100 + i) for i in range(20)])
    btc = _serie("BTCUSDT", [str(100 + 3 * i) for i in range(20)])
    assert calculate_ratio_rsi(simbolo, btc) == Decimal(0)


def test_historia_comun_insuficiente_devuelve_none():
    simbolo = _serie("LTCUSDT", ["100"] * 14)  # periodo 14 exige >= 15 velas
    btc = _serie("BTCUSDT", ["100"] * 14)
    assert calculate_ratio_rsi(simbolo, btc) is None


def test_solo_cuenta_la_historia_en_comun_sin_rellenar_velas_faltantes():
    simbolo = _serie("LTCUSDT", ["100"] * 20)
    btc = _serie("BTCUSDT", ["100"] * 10)  # solo 10 velas en común
    assert calculate_ratio_rsi(simbolo, btc) is None


def test_cierre_de_btc_en_cero_devuelve_none():
    simbolo = _serie("LTCUSDT", ["100"] * 20)
    btc = _serie("BTCUSDT", ["100"] * 19 + ["0"])
    assert calculate_ratio_rsi(simbolo, btc) is None
