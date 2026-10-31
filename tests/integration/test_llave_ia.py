"""Verificación previa de la llave (`verificar_llave_ia`) contra Postgres real: el aviso queda
guardado una sola vez por día aunque el ciclo horario reintente."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.ai.client import LlaveIARechazada
from app.db.session import session_scope
from app.domain.enums import TipoAlerta
from app.orchestration.ai_budget import verificar_llave_ia
from app.repositories.alertas import list_alertas

AHORA = datetime(2026, 10, 31, 0, 5, tzinfo=timezone.utc)
AJUSTES = SimpleNamespace(anthropic_api_key="sk-ant-nada")


def _avisos(fabrica) -> list:
    with session_scope(fabrica) as s:
        return [a for a in list_alertas(s, limit=100) if a.tipo == TipoAlerta.LLAVE_IA_RECHAZADA]


def _rechaza(_settings):
    raise LlaveIARechazada("Anthropic rechazó la llave (HTTP 401)")


def _acepta(_settings):
    return None


def test_llave_aceptada_deja_iniciar_sin_aviso(reinicios):
    assert verificar_llave_ia(reinicios, AJUSTES, now=AHORA, verificador=_acepta) is True
    assert _avisos(reinicios) == []


def test_llave_rechazada_bloquea_y_deja_un_aviso_sin_secretos(reinicios):
    assert verificar_llave_ia(reinicios, AJUSTES, now=AHORA, verificador=_rechaza) is False
    (aviso,) = _avisos(reinicios)
    assert "LLAVE DE IA RECHAZADA" in aviso.message and "HTTP 401" in aviso.message
    assert "sk-ant" not in aviso.message
    assert aviso.dedup_key == "llave-ia-rechazada:2026-10-31"


def test_los_reintentos_del_ciclo_horario_no_repiten_el_aviso_del_mismo_dia(reinicios):
    for hora in range(1, 6):  # el horario reintenta cada hora mientras la llave siga mal
        assert verificar_llave_ia(reinicios, AJUSTES, now=AHORA + timedelta(hours=hora), verificador=_rechaza) is False
    assert len(_avisos(reinicios)) == 1


def test_al_dia_siguiente_vuelve_a_avisar_si_sigue_rota(reinicios):
    verificar_llave_ia(reinicios, AJUSTES, now=AHORA, verificador=_rechaza)
    verificar_llave_ia(reinicios, AJUSTES, now=AHORA + timedelta(days=1), verificador=_rechaza)
    assert len(_avisos(reinicios)) == 2


def test_un_fallo_pasajero_no_bloquea_ni_avisa(reinicios):
    def _red_caida(_settings):
        raise ConnectionError("sin red")

    assert verificar_llave_ia(reinicios, AJUSTES, now=AHORA, verificador=_red_caida) is True
    assert _avisos(reinicios) == []
