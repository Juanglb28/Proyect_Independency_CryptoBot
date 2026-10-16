"""Reglas puras de recuperación al arrancar (`app/domain/rules/recovery.py`)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.domain.rules.recovery import (
    HORA_CICLO_DIARIO_UTC,
    MENSAJE_CICLO_INTERRUMPIDO,
    CicloParaRecuperar,
    hay_ciclo_diario_perdido,
    inicio_del_dia_utc,
    mensaje_ciclo_diario_perdido,
)

HOY = datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc)


def _ciclo(inicio: datetime, *, fin: datetime | None, error: str | None = None) -> CicloParaRecuperar:
    return CicloParaRecuperar(uuid.uuid4(), inicio, fin, error)


def _completo(inicio: datetime) -> CicloParaRecuperar:
    return _ciclo(inicio, fin=inicio + timedelta(minutes=9))


def test_la_hora_programada_es_00_05_utc():
    assert HORA_CICLO_DIARIO_UTC == time(0, 5)


def test_sin_ciclos_despues_de_la_hora_programada_esta_perdido():
    assert hay_ciclo_diario_perdido([], now=HOY) is True


def test_justo_en_la_hora_programada_sin_ciclo_ya_cuenta_como_perdido():
    assert hay_ciclo_diario_perdido([], now=datetime(2026, 10, 10, 0, 5, tzinfo=timezone.utc)) is True


def test_un_minuto_antes_de_la_hora_programada_todavia_no_falta_nada():
    assert hay_ciclo_diario_perdido([], now=datetime(2026, 10, 10, 0, 4, 59, tzinfo=timezone.utc)) is False


def test_un_ciclo_completo_de_hoy_no_esta_perdido():
    assert hay_ciclo_diario_perdido([_completo(datetime(2026, 10, 10, 0, 5, tzinfo=timezone.utc))], now=HOY) is False


def test_un_ciclo_completo_disparado_a_mitad_de_dia_tambien_cuenta():
    assert hay_ciclo_diario_perdido([_completo(datetime(2026, 10, 10, 2, 5, tzinfo=timezone.utc))], now=HOY) is False


def test_el_ciclo_completo_de_ayer_no_cuenta_para_hoy():
    ayer = datetime(2026, 10, 9, 23, 59, tzinfo=timezone.utc)
    assert hay_ciclo_diario_perdido([_completo(ayer)], now=HOY) is True


def test_un_ciclo_sin_terminar_no_cuenta_como_completo():
    sin_terminar = _ciclo(datetime(2026, 10, 10, 0, 5, tzinfo=timezone.utc), fin=None)
    assert hay_ciclo_diario_perdido([sin_terminar], now=HOY) is True


def test_un_ciclo_interrumpido_no_cuenta_como_completo():
    inicio = datetime(2026, 10, 10, 0, 5, tzinfo=timezone.utc)
    interrumpido = _ciclo(inicio, fin=inicio + timedelta(hours=1), error=MENSAJE_CICLO_INTERRUMPIDO)
    assert hay_ciclo_diario_perdido([interrumpido], now=HOY) is True


def test_un_ciclo_fallido_no_cuenta_pero_uno_posterior_completo_si():
    inicio = datetime(2026, 10, 10, 0, 5, tzinfo=timezone.utc)
    fallido = _ciclo(inicio, fin=inicio + timedelta(minutes=2), error="RuntimeError: boom")
    assert hay_ciclo_diario_perdido([fallido], now=HOY) is True
    assert hay_ciclo_diario_perdido([fallido, _completo(inicio + timedelta(hours=1))], now=HOY) is False


def test_now_sin_zona_horaria_se_rechaza():
    with pytest.raises(ValueError):
        hay_ciclo_diario_perdido([], now=datetime(2026, 10, 10, 3, 0))


def test_now_en_otra_zona_horaria_se_rechaza():
    with pytest.raises(ValueError):
        inicio_del_dia_utc(datetime(2026, 10, 10, 3, 0, tzinfo=timezone(timedelta(hours=-5))))


def test_inicio_del_dia_utc():
    assert inicio_del_dia_utc(HOY) == datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc)


def test_mensaje_menciona_la_fecha_y_que_no_se_relanzo():
    texto = mensaje_ciclo_diario_perdido(date(2026, 10, 10), interrumpidos=0)
    assert "2026-10-10" in texto and "NO se relanzó" in texto and "interrumpidos" not in texto


def test_mensaje_menciona_los_ciclos_interrumpidos():
    assert "2 ciclo(s) a medias" in mensaje_ciclo_diario_perdido(date(2026, 10, 10), interrumpidos=2)
