"""Rango de descarga del backfill (scripts.backfill_candles)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.backfill_candles import calcular_rango, main, parse_args

AHORA = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def test_por_defecto_baja_dos_anios_hasta_el_presente():
    args = parse_args([])
    assert args.hasta_dias == 0
    assert calcular_rango(AHORA, args.days, args.hasta_dias) == (AHORA - timedelta(days=730), AHORA)


def test_ampliar_hacia_atras_descarga_solo_el_tramo_viejo():
    desde, hasta = calcular_rango(AHORA, 1825, 740)
    assert desde == AHORA - timedelta(days=1825) and hasta == AHORA - timedelta(days=740)
    assert desde.tzinfo is not None and hasta.tzinfo is not None


@pytest.mark.parametrize("days,hasta", [(100, 100), (100, 150), (100, -1), (0, 0)])
def test_rangos_invalidos_se_rechazan(days, hasta):
    with pytest.raises(ValueError):
        calcular_rango(AHORA, days, hasta)


def test_main_rechaza_un_rango_invalido_antes_de_tocar_la_base(capsys):
    assert main(["--days", "10", "--hasta-dias", "20"]) == 1
    assert "debe ser >= 0 y menor que --days" in capsys.readouterr().err
