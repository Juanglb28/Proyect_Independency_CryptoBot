"""Test de integración de app.repositories.ai_calls contra Postgres real.

`save_llamada_ia` ya se ejerce indirectamente en test_ai_agents.py /
test_consolidator_agent.py (con `save_llamada_ia` parcheado), pero nunca
contra la base real — este archivo cubre ambas funciones del módulo tal
como quedan persistidas de verdad, en particular `get_gasto_acumulado_desde`
(bug real 2026-10-02: ver app/config.py::anthropic_saldo_declarado_usd).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.repositories.ai_calls import LlamadaIARow, get_gasto_acumulado_desde, save_llamada_ia


def _row(cost_usd: Decimal) -> LlamadaIARow:
    return LlamadaIARow(
        agente="analista_cuantitativo",
        modelo="claude-sonnet-5-5",
        prompt_version="v1",
        input_tokens=100,
        output_tokens=50,
        cost_usd=cost_usd,
        respuesta={"ok": True},
    )


def test_get_gasto_acumulado_desde_suma_solo_las_llamadas_posteriores_a_la_fecha(db_session):
    save_llamada_ia(db_session, _row(Decimal("0.10")))
    save_llamada_ia(db_session, _row(Decimal("0.25")))

    desde = datetime.now(timezone.utc) - timedelta(minutes=1)
    total = get_gasto_acumulado_desde(db_session, desde)

    assert total == Decimal("0.35")


def test_get_gasto_acumulado_desde_ignora_llamadas_anteriores_a_la_fecha(db_session):
    save_llamada_ia(db_session, _row(Decimal("0.10")))

    desde_el_futuro = datetime.now(timezone.utc) + timedelta(days=1)
    total = get_gasto_acumulado_desde(db_session, desde_el_futuro)

    assert total == Decimal("0")


def test_get_gasto_acumulado_desde_sin_llamadas_es_cero(db_session):
    desde = datetime.now(timezone.utc) - timedelta(days=1)
    total = get_gasto_acumulado_desde(db_session, desde)

    assert total == Decimal("0")
