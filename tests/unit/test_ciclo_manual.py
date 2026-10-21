"""Tests de `app.domain.rules.ciclo_manual`: regla pura que decide si un ciclo
manual debe pedir confirmación porque ya hay uno reciente (2026-10-21)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.rules.ciclo_manual import (
    VENTANA_CICLO_RECIENTE,
    CicloReciente,
    buscar_ciclo_reciente,
    mensaje_ciclo_reciente,
)
from app.domain.rules.recovery import CicloParaRecuperar

AHORA = datetime(2026, 10, 20, 3, 30, tzinfo=timezone.utc)


def _ciclo(*, inicio, fin, error=None) -> CicloParaRecuperar:
    return CicloParaRecuperar(id=uuid.uuid4(), started_at=inicio, finished_at=fin, error_message=error)


def test_sin_ciclos_no_hay_ciclo_reciente():
    assert buscar_ciclo_reciente([], now=AHORA) is None


def test_ciclo_completado_hace_18_minutos_es_reciente():
    c = _ciclo(inicio=AHORA - timedelta(minutes=24), fin=AHORA - timedelta(minutes=18))
    r = buscar_ciclo_reciente([c], now=AHORA)
    assert r == CicloReciente(inicio=c.started_at, fin=c.finished_at)
    assert not r.en_curso


def test_ciclo_fuera_de_la_ventana_no_cuenta():
    c = _ciclo(inicio=AHORA - timedelta(hours=7), fin=AHORA - timedelta(hours=6, minutes=55))
    assert buscar_ciclo_reciente([c], now=AHORA) is None


def test_borde_de_la_ventana_es_inclusivo():
    justo = _ciclo(inicio=AHORA - VENTANA_CICLO_RECIENTE - timedelta(minutes=5), fin=AHORA - VENTANA_CICLO_RECIENTE)
    un_segundo_antes = _ciclo(
        inicio=AHORA - VENTANA_CICLO_RECIENTE - timedelta(minutes=5),
        fin=AHORA - VENTANA_CICLO_RECIENTE - timedelta(seconds=1),
    )
    assert buscar_ciclo_reciente([justo], now=AHORA) is not None
    assert buscar_ciclo_reciente([un_segundo_antes], now=AHORA) is None


def test_se_mide_desde_el_fin_no_desde_el_inicio():
    """Un ciclo largo que empezó hace más de la ventana pero terminó dentro de ella cuenta."""
    c = _ciclo(inicio=AHORA - VENTANA_CICLO_RECIENTE - timedelta(minutes=1), fin=AHORA - timedelta(minutes=30))
    assert buscar_ciclo_reciente([c], now=AHORA) is not None


def test_ciclo_fallido_no_cuenta():
    c = _ciclo(inicio=AHORA - timedelta(minutes=30), fin=AHORA - timedelta(minutes=25), error="boom")
    assert buscar_ciclo_reciente([c], now=AHORA) is None


def test_ciclo_en_curso_reciente_cuenta_y_se_marca_en_curso():
    c = _ciclo(inicio=AHORA - timedelta(minutes=2), fin=None)
    r = buscar_ciclo_reciente([c], now=AHORA)
    assert r is not None and r.en_curso and r.referencia == c.started_at


def test_ciclo_sin_terminar_muy_viejo_no_cuenta():
    """Un ciclo a medias de hace días es un proceso muerto, no uno en curso."""
    c = _ciclo(inicio=AHORA - timedelta(days=2), fin=None)
    assert buscar_ciclo_reciente([c], now=AHORA) is None


def test_ciclo_con_inicio_en_el_futuro_se_ignora():
    c = _ciclo(inicio=AHORA + timedelta(minutes=1), fin=None)
    assert buscar_ciclo_reciente([c], now=AHORA) is None


def test_devuelve_el_mas_reciente():
    viejo = _ciclo(inicio=AHORA - timedelta(hours=3), fin=AHORA - timedelta(hours=2, minutes=55))
    nuevo = _ciclo(inicio=AHORA - timedelta(minutes=24), fin=AHORA - timedelta(minutes=18))
    r = buscar_ciclo_reciente([nuevo, viejo], now=AHORA)
    assert r.fin == nuevo.finished_at


def test_un_fallido_mas_nuevo_no_tapa_a_un_completado_reciente():
    ok = _ciclo(inicio=AHORA - timedelta(hours=1), fin=AHORA - timedelta(minutes=55))
    fallido = _ciclo(inicio=AHORA - timedelta(minutes=10), fin=AHORA - timedelta(minutes=8), error="boom")
    assert buscar_ciclo_reciente([ok, fallido], now=AHORA).fin == ok.finished_at


def test_exige_now_con_utc():
    with pytest.raises(ValueError, match="UTC"):
        buscar_ciclo_reciente([], now=datetime(2026, 10, 20, 3, 30))
    with pytest.raises(ValueError, match="UTC"):
        buscar_ciclo_reciente([], now=datetime(2026, 10, 20, 3, 30, tzinfo=timezone(timedelta(hours=-5))))


def test_ventana_no_positiva_se_rechaza():
    with pytest.raises(ValueError, match="positiva"):
        buscar_ciclo_reciente([], now=AHORA, ventana=timedelta(0))


def test_mensaje_de_ciclo_completado_dice_hace_cuanto():
    r = CicloReciente(inicio=AHORA - timedelta(minutes=24), fin=AHORA - timedelta(minutes=18))
    texto = mensaje_ciclo_reciente(r, now=AHORA)
    assert "completado hace 18 min" in texto and "UTC" in texto


def test_mensaje_con_horas_y_minutos():
    r = CicloReciente(inicio=AHORA - timedelta(hours=3), fin=AHORA - timedelta(hours=2, minutes=5))
    assert "hace 2 h 5 min" in mensaje_ciclo_reciente(r, now=AHORA)


def test_mensaje_de_ciclo_en_curso():
    r = CicloReciente(inicio=AHORA - timedelta(minutes=2), fin=None)
    texto = mensaje_ciclo_reciente(r, now=AHORA)
    assert "EN CURSO" in texto and "hace 2 min" in texto


def test_la_ventana_inicial_es_de_6_horas():
    """Fija el valor propuesto: cambiarlo debe ser una decisión consciente."""
    assert VENTANA_CICLO_RECIENTE == timedelta(hours=6)
