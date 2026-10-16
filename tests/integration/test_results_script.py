"""CLI de resultados (`scripts/results.py`): solo imprime lo que calculan los servicios."""

from __future__ import annotations

from decimal import Decimal

from scripts.results import main, parse_args, run_command
from tests.integration.test_results_pipeline import CAPITAL, _abrir, _t, _tick, _universo, seis_operaciones  # noqa: F401

D = Decimal


def _correr(db_session, *argv, dias=40):
    return run_command(db_session, parse_args(list(argv)), now=_t(0, dias), initial_capital=CAPITAL)


def test_sin_operaciones_los_comandos_lo_dicen(db_session):
    assert _correr(db_session, "trades") == ["Todavía no hay operaciones cerradas."]
    assert any("MUESTRA_INSUFICIENTE" in l for l in _correr(db_session, "validate"))
    assert any("enmienda E4" in linea for linea in _correr(db_session, "validate"))
    lineas = _correr(db_session, "performance")
    assert lineas[0].startswith("Periodo:") and "operaciones=0" in lineas[1]


def test_trades_lista_cada_operacion_cerrada(db_session, seis_operaciones):  # noqa: F811
    lineas = _correr(db_session, "trades")
    assert len(lineas) == 6
    assert "BTCUSDT" in lineas[0] and "CONFIRMED TRAILING_STOP" in lineas[0]
    assert "INVALIDATED STOP_LOSS" in lineas[-1]


def test_performance_muestra_y_guarda_las_metricas(db_session, seis_operaciones):  # noqa: F811
    lineas = _correr(db_session, "performance")
    texto = "\n".join(lineas)
    assert "[GLOBAL] operaciones=6" in texto and "win rate=50.00%" in texto
    assert "[SYMBOL BTCUSDT]" in texto and "[SCORE_TERCIL SUPERIOR]" in texto
    assert "discriminación=20.38" in texto
    assert _correr(db_session, "performance", "--days", "60")[0].startswith("Periodo:")


def test_validate_imprime_el_intervalo_y_la_advertencia_de_muestra_preliminar(db_session, seis_operaciones):  # noqa: F811
    texto = "\n".join(_correr(db_session, "validate"))
    assert "PRELIMINAR" in texto and "bootstrap (1000 iteraciones" in texto and "orientativo" in texto
    assert "Tercil de 2 operaciones" in texto
    assert "estratificada por mes (enmienda E4)" in texto


def test_performance_sin_operaciones_no_imprime_notacion_cientifica(db_session):
    texto = "\n".join(_correr(db_session, "performance"))
    assert "0E-" not in texto and "P&L neto=0.00 USD (bruto 0.00, comisiones 0.00)" in texto


# --- corrección auditable (CLI) ---

from tests.integration.test_results_schema import _cerrar  # noqa: E402


def test_corregir_uuid_invalido_lo_dice(db_session):
    assert any("inválido" in linea for linea in _correr(db_session, "corregir", "--position", "no-uuid", "--motivo", "x"))


def test_corregir_sin_resultado_lo_dice(db_session):
    import uuid

    salida = _correr(db_session, "corregir", "--position", str(uuid.uuid4()), "--motivo", "x")
    assert any("no tiene un resultado" in linea for linea in salida)


def test_corregir_sin_cambios_no_escribe(db_session):
    pos = _cerrar(db_session)
    salida = _correr(db_session, "corregir", "--position", str(pos.id), "--motivo", "revisión")
    assert any("coincide con la versión vigente" in linea for linea in salida)


def test_versiones_muestra_el_original(db_session):
    pos = _cerrar(db_session)
    salida = "\n".join(_correr(db_session, "versiones", "--position", str(pos.id)))
    assert "1 versión/es" in salida and "original" in salida and "VIGENTE" in salida


def test_corregir_requiere_motivo_en_argparse():
    import pytest

    with pytest.raises(SystemExit):
        parse_args(["corregir", "--position", "x"])  # falta --motivo
