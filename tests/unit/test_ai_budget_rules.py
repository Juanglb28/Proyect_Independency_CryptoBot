"""Reglas puras del tope diario de gasto de IA (`app/domain/rules/ai_budget.py`)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.config import Settings
from app.domain.rules.ai_budget import mensaje_presupuesto_ia_agotado, presupuesto_diario_agotado

D = Decimal


def test_por_debajo_del_tope_no_esta_agotado():
    assert presupuesto_diario_agotado(D("1.99"), D("2.00")) is False


def test_exactamente_en_el_tope_ya_cuenta_como_agotado():
    assert presupuesto_diario_agotado(D("2.00"), D("2")) is True


def test_por_encima_del_tope_esta_agotado():
    assert presupuesto_diario_agotado(D("2.4731"), D("2.00")) is True


def test_sin_gasto_no_esta_agotado():
    assert presupuesto_diario_agotado(D("0"), D("2.00")) is False


@pytest.mark.parametrize("tope", [D("0"), D("-1")])
def test_un_tope_no_positivo_se_rechaza(tope):
    with pytest.raises(ValueError):
        presupuesto_diario_agotado(D("0"), tope)


def test_el_mensaje_dice_gasto_tope_dia_y_que_los_stops_siguen():
    texto = mensaje_presupuesto_ia_agotado(date(2026, 10, 10), gasto_hoy_usd=D("2.4731"), tope_usd=D("2"))
    assert "2026-10-10" in texto and "2.47" in texto and "2.00" in texto
    assert "stops siguen" in texto


def test_el_tope_por_defecto_es_2_usd_y_debe_ser_positivo(monkeypatch):
    monkeypatch.delenv("AI_DAILY_BUDGET_USD", raising=False)
    base = {"database_url": "postgresql+psycopg://u:p@h/db"}
    assert Settings(_env_file=None, **base).ai_daily_budget_usd == D("2.00")
    with pytest.raises(ValueError):
        Settings(_env_file=None, ai_daily_budget_usd=D("0"), **base)


# ── Guardia de saldo (2026-10-11) ───────────────────────────────────────────

import pytest  # noqa: E402
from datetime import date as _date  # noqa: E402

from app.domain.rules.ai_budget import (  # noqa: E402
    mensaje_saldo_ia_insuficiente,
    saldo_estimado_usd,
    saldo_insuficiente,
)


def test_saldo_estimado_es_declarado_menos_gastado():
    assert saldo_estimado_usd(Decimal("5.50"), Decimal("4.865250")) == Decimal("0.634750")


def test_saldo_menor_que_el_minimo_es_insuficiente():
    assert saldo_insuficiente(Decimal("0.34"), Decimal("0.60")) is True


def test_saldo_igual_al_minimo_alcanza():
    assert saldo_insuficiente(Decimal("0.60"), Decimal("0.60")) is False


def test_un_centavo_menos_que_el_minimo_no_alcanza():
    assert saldo_insuficiente(Decimal("0.59"), Decimal("0.60")) is True


def test_saldo_negativo_estimado_es_insuficiente():
    assert saldo_insuficiente(Decimal("-0.10"), Decimal("0.60")) is True


@pytest.mark.parametrize("minimo", [Decimal("0"), Decimal("-1")])
def test_minimo_no_positivo_se_rechaza(minimo):
    with pytest.raises(ValueError):
        saldo_insuficiente(Decimal("1"), minimo)


def test_mensaje_de_saldo_dice_cifras_dia_y_que_es_estimacion():
    texto = mensaje_saldo_ia_insuficiente(
        _date(2026, 10, 11), saldo_estimado=Decimal("0.3412"), minimo_por_ciclo_usd=Decimal("0.6")
    )
    assert "SALDO DE IA INSUFICIENTE" in texto
    assert "0.34 USD" in texto and "0.60 USD" in texto and "2026-10-11" in texto
    assert "estimación" in texto and "ANTHROPIC_SALDO_DECLARADO_USD" in texto
    assert "monitor y los stops siguen funcionando" in texto
