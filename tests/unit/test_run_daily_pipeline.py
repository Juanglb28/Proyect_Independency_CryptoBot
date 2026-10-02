"""Test de scripts.run_daily_pipeline: solo la lógica de encadenado (parar
en el primer paso que falle), sin tocar red, Binance, Postgres ni
Anthropic — cada paso real ya tiene su propia cobertura por separado.
"""

from __future__ import annotations

from types import SimpleNamespace

from scripts import run_daily_pipeline


def test_corre_los_tres_pasos_en_orden_si_todos_tienen_exito(monkeypatch):
    comandos_corridos: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        comandos_corridos.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(run_daily_pipeline.subprocess, "run", fake_run)

    codigo = run_daily_pipeline.main()

    assert codigo == 0
    assert len(comandos_corridos) == 3
    assert comandos_corridos[0][2:] == ["scripts.backfill_candles", "--days", "5"]
    assert comandos_corridos[1][2:] == ["scripts.calculate_indicators"]
    assert comandos_corridos[2][2:] == ["scripts.run_daily_cycle"]


def test_se_detiene_en_el_primer_paso_que_falla_sin_correr_los_siguientes(monkeypatch):
    comandos_corridos: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        comandos_corridos.append(cmd)
        # El primer paso (backfill) falla; nunca debe llegar a calcular
        # indicadores ni a correr el ciclo con datos a medio actualizar.
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(run_daily_pipeline.subprocess, "run", fake_run)

    codigo = run_daily_pipeline.main()

    assert codigo == 1
    assert len(comandos_corridos) == 1
    assert comandos_corridos[0][2:] == ["scripts.backfill_candles", "--days", "5"]
