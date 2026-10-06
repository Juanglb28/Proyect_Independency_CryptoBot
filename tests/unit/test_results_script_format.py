"""El formato de pantalla de scripts.results no cambia valores, solo la presentación."""
from decimal import Decimal

from scripts.results import _fmt


def test_cero_decimal_no_usa_notacion_cientifica():
    assert _fmt(Decimal("0E-8")) == "0.00"


def test_redondea_a_dos_decimales_solo_para_mostrar():
    assert _fmt(Decimal("20.379600"), " USD") == "20.38 USD"


def test_precios_conservan_todos_los_decimales_sin_ceros_de_relleno():
    assert _fmt(Decimal("0.000012340000"), decimales=None) == "0.00001234"
    assert _fmt(Decimal("65000.50000000"), decimales=None) == "65000.5"


def test_none_es_nd():
    assert _fmt(None) == "n/d"
