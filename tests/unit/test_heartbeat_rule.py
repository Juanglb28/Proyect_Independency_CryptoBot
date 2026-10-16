"""Tests de la regla pura de latido del scheduler (app.domain.rules.heartbeat)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.domain.rules.heartbeat import (
    UMBRAL_LATIDO_VENCIDO_MINUTOS,
    interpretar_latido,
)

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)


def _latido(minutos_atras: float, *, pid: int | None = 123) -> str:
    ts = NOW - timedelta(minutes=minutos_atras)
    payload: dict = {"status": "alive", "timestamp": ts.isoformat()}
    if pid is not None:
        payload["pid"] = pid
    return json.dumps(payload)


def test_latido_fresco_no_esta_vencido():
    r = interpretar_latido(_latido(2), now=NOW)
    assert r.disponible and not r.vencido
    assert r.pid == 123 and r.edad == timedelta(minutes=2)


def test_latido_viejo_esta_vencido():
    r = interpretar_latido(_latido(20), now=NOW)
    assert r.disponible and r.vencido and r.edad == timedelta(minutes=20)


def test_el_umbral_es_estricto_justo_en_el_limite():
    # Exactamente en el umbral NO está vencido; un instante después, sí.
    assert not interpretar_latido(_latido(UMBRAL_LATIDO_VENCIDO_MINUTOS), now=NOW).vencido
    assert interpretar_latido(_latido(UMBRAL_LATIDO_VENCIDO_MINUTOS + 0.1), now=NOW).vencido


def test_umbral_personalizado():
    assert interpretar_latido(_latido(6), now=NOW, umbral_minutos=5).vencido
    assert not interpretar_latido(_latido(4), now=NOW, umbral_minutos=5).vencido


def test_texto_none_no_disponible_y_vencido():
    r = interpretar_latido(None, now=NOW)
    assert not r.disponible and r.vencido and r.timestamp is None and r.edad is None


def test_json_invalido_no_disponible_y_vencido():
    r = interpretar_latido("{no es json", now=NOW)
    assert not r.disponible and r.vencido


def test_sin_timestamp_no_disponible_y_vencido():
    r = interpretar_latido(json.dumps({"status": "alive", "pid": 1}), now=NOW)
    assert not r.disponible and r.vencido


def test_timestamp_no_parseable_no_disponible_y_vencido():
    r = interpretar_latido(json.dumps({"timestamp": "ayer por la tarde"}), now=NOW)
    assert not r.disponible and r.vencido


def test_json_que_no_es_objeto_no_disponible_y_vencido():
    r = interpretar_latido(json.dumps([1, 2, 3]), now=NOW)
    assert not r.disponible and r.vencido


def test_sin_pid_sigue_disponible():
    r = interpretar_latido(_latido(1, pid=None), now=NOW)
    assert r.disponible and r.pid is None and not r.vencido
