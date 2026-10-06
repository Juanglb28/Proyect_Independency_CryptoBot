"""Reescalado de series para comparar equity contra BTC (solo presentación)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.domain.rules.results import PuntoDeSerie, scale_series_to_base

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_el_primer_punto_vale_la_base_y_el_resto_es_proporcional():
    serie = [PuntoDeSerie(T0 + timedelta(hours=i), Decimal(v)) for i, v in enumerate((50000, 55000, 45000))]
    escalada = scale_series_to_base(serie, Decimal(1000))
    assert [p.value for p in escalada] == [Decimal(1000), Decimal(1100), Decimal(900)]
    assert [p.time for p in escalada] == [p.time for p in serie]


def test_ordena_por_tiempo_antes_de_reescalar():
    serie = [PuntoDeSerie(T0 + timedelta(hours=1), Decimal(200)), PuntoDeSerie(T0, Decimal(100))]
    assert [p.value for p in scale_series_to_base(serie, Decimal(1000))] == [Decimal(1000), Decimal(2000)]


def test_serie_vacia_o_con_primer_valor_cero_no_inventa_nada():
    assert scale_series_to_base([], Decimal(1000)) == []
    assert scale_series_to_base([PuntoDeSerie(T0, Decimal(0))], Decimal(1000)) == []


def test_classify_outcome_usa_la_misma_regla_que_performance():
    from app.domain.enums import ResultadoOperacion
    from app.domain.rules.results import classify_outcome

    assert classify_outcome(Decimal("0.01")) == ResultadoOperacion.GANADORA
    assert classify_outcome(Decimal("-0.01")) == ResultadoOperacion.PERDEDORA
    assert classify_outcome(Decimal("0")) == ResultadoOperacion.EQUILIBRIO
