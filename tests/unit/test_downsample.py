from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.rules.results import PuntoDeSerie
from app.services._series import downsample

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _serie(valores):
    return [PuntoDeSerie(T0 + timedelta(minutes=i), Decimal(v)) for i, v in enumerate(valores)]


def test_serie_corta_se_devuelve_completa():
    s = _serie([1, 2, 3])
    assert downsample(s, 10) == tuple(s)


def test_conserva_el_primero_y_el_ultimo_y_respeta_el_maximo():
    s = _serie(range(1000))
    r = downsample(s, 50)
    assert len(r) <= 50 and r[0] == s[0] and r[-1] == s[-1]
    assert [p.time for p in r] == sorted(p.time for p in r)


def test_minimo_no_pierde_la_peor_caida():
    valores = [0] * 200
    valores[137] = -15
    r = downsample(_serie(valores), 20, conservar="minimo")
    assert min(p.value for p in r) == Decimal(-15)


def test_max_puntos_invalido():
    with pytest.raises(ValueError):
        downsample(_serie([1, 2, 3]), 1)
