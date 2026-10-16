"""Script `scripts.backtest_walk_forward`: argumentos y formato del informe (sin base de datos)."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.rules.walk_forward import ObservacionBacktest, informe_walk_forward
from scripts import backtest_walk_forward as script


def test_un_simbolo_fuera_de_la_lista_autorizada_se_rechaza_sin_tocar_la_base(capsys):
    assert script.main(["--symbols", "DOGEFAKEUSDT"]) == 1
    assert "no está en la lista autorizada" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["--entrenamiento", "0"], ["--prueba", "-1"], ["--paso", "0"]])
def test_ventanas_invalidas_se_rechazan(args, capsys):
    assert script.main(args) == 1
    assert "debe ser >= 1" in capsys.readouterr().err


def test_el_informe_impreso_muestra_ventanas_veredicto_y_limitaciones(capsys):
    rng = random.Random(3)
    inicio = date(2025, 1, 1)
    obs = [
        ObservacionBacktest(
            f"S{s}", inicio + timedelta(days=d), Decimal(rng.randint(0, 100)), Decimal(rng.randint(-40, 60)) / 1000,
            datetime.combine(inicio + timedelta(days=d + 2), datetime.min.time(), tzinfo=timezone.utc),
        )
        for d in range(520) for s in range(3)
    ]
    script.imprimir(informe_walk_forward(obs), len(obs), 3)
    salida = capsys.readouterr().out
    for esperado in ("entrenamiento 12 m / prueba 3 m / paso 3 m", "Pliegue 1", "PRUEBA AGREGADA", "VEREDICTO:", "optimista", "DETERMINISTA"):
        assert esperado in salida


def test_el_script_de_diagnosticos_valida_argumentos_e_imprime_las_cuatro_secciones(capsys):
    from scripts import backtest_diagnosticos as diag
    from app.domain.rules.walk_forward_diagnostics import FACTORES_DIAGNOSTICO, diagnosticos

    assert diag.main(["--symbols", "NOPE"]) == 1
    assert "no está en la lista autorizada" in capsys.readouterr().err

    rng = random.Random(9)
    inicio = date(2025, 1, 1)
    obs = []
    for d in range(520):
        dia = inicio + timedelta(days=d)
        fin = datetime.combine(dia + timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc)
        for s in range(10):
            sub = {f: Decimal(rng.randint(0, 100)) for f in FACTORES_DIAGNOSTICO}
            r = Decimal(rng.randint(-40, 60)) / 1000
            obs.append(ObservacionBacktest(f"S{s}", dia, sum(sub.values(), Decimal(0)) / 4, r, fin,
                                           factores=tuple(sub.items()), retorno_marcado=r, marcado_en=fin))
    diag.imprimir(diagnosticos(obs))
    salida = capsys.readouterr().out
    for esperado in ("1. SESGO DE CENSURA", "2. POR FACTOR", "factor: rsi", "3. RANGO DENTRO DEL DÍA", "4. SENSIBILIDAD DE VENTANAS (9/3/3", "EXPLORATORIO"):
        assert esperado in salida
    assert "→ 2026-" in salida or "→ 2025-" in salida  # cada prueba muestra sus fechas


def test_hasta_fecha_exige_una_fecha_valida(capsys):
    with pytest.raises(SystemExit):
        script.main(["--hasta-fecha", "no-es-fecha"])
