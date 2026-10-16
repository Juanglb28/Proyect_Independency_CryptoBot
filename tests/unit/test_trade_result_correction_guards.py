"""Guardas de la corrección auditable que no necesitan base de datos: el motivo
obligatorio y la validación de columnas se comprueban ANTES de tocar la sesión."""

from __future__ import annotations

import uuid

import pytest

from app.repositories.trade_results import insertar_correccion


def test_insertar_correccion_exige_motivo_no_vacio():
    for motivo in ("", "   "):
        with pytest.raises(ValueError, match="motivo"):
            insertar_correccion(None, supersedes_id=uuid.uuid4(), valores={}, motivo=motivo)


def test_insertar_correccion_rechaza_columnas_desconocidas():
    with pytest.raises(ValueError, match="columnas desconocidas"):
        insertar_correccion(None, supersedes_id=uuid.uuid4(), valores={"inventada": 1}, motivo="ok")


def test_insertar_correccion_rechaza_columnas_de_versionado_en_valores():
    # supersedes_id/correction_reason/corrected_by los pone la función, no el llamador.
    with pytest.raises(ValueError, match="columnas desconocidas"):
        insertar_correccion(None, supersedes_id=uuid.uuid4(), valores={"supersedes_id": uuid.uuid4()}, motivo="ok")


def test_corregir_trade_result_exige_motivo_no_vacio():
    from decimal import Decimal

    from app.results.trade_result_service import corregir_trade_result

    with pytest.raises(ValueError, match="motivo"):
        corregir_trade_result(None, uuid.uuid4(), motivo="  ", initial_capital=Decimal("1000"))
