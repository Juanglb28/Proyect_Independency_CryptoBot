"""Test de `scripts.run_daily_cycle.explicar_sin_propuestas`: solo el texto que
explica por qué un ciclo terminó sin propuestas — sin base de datos, red ni
Anthropic (observación 2026-10-04: la salida solo decía "Sin propuestas este
ciclo." sin distinguir las causas posibles)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.orchestration.daily_cycle import DailyCycleResult
from scripts.run_daily_cycle import explicar_sin_propuestas


def _result(**overrides) -> DailyCycleResult:
    defaults = dict(
        ciclo_id=uuid.uuid4(),
        candidatos_evaluados=("ETHUSDT",),
        candidatos_excluidos_por_calidad=(),
        propuesta_ids=(),
        cost_usd=Decimal("0.1"),
    )
    defaults.update(overrides)
    return DailyCycleResult(**defaults)


def test_sin_candidatos_del_prefiltro():
    texto = "\n".join(explicar_sin_propuestas(_result(candidatos_evaluados=())))
    assert "pre-filtro" in texto


def test_todos_excluidos_por_calidad_no_llamo_al_consolidador():
    texto = "\n".join(
        explicar_sin_propuestas(
            _result(candidatos_evaluados=("ETHUSDT",), candidatos_excluidos_por_calidad=("ETHUSDT",))
        )
    )
    assert "calidad de datos" in texto
    assert "no se llamo" in texto


def test_consolidador_no_invertir_muestra_su_razon_y_confianza():
    texto = "\n".join(
        explicar_sin_propuestas(
            _result(
                consolidador_recomendacion="no_invertir",
                consolidador_razon_no_invertir="ningun candidato sostiene una tesis",
                consolidador_confianza_global="alta",
            )
        )
    )
    assert "NO invertir" in texto
    assert "ningun candidato sostiene una tesis" in texto
    assert "alta" in texto


def test_caso_inesperado_no_inventa_una_causa():
    texto = "\n".join(explicar_sin_propuestas(_result(consolidador_recomendacion="invertir")))
    assert "no determinado" in texto
