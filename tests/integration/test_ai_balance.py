"""Guardia de saldo de IA (`verificar_saldo_ia`) contra Postgres real.

`llamadas_ia.created_at` lo pone la base (`now()`), así que "gasto desde la declaración"
se controla con la fecha declarada: una fecha pasada incluye lo gastado en el test, una
futura no incluye nada.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.db.session import session_scope
from app.domain.enums import TipoAlerta
from app.orchestration.ai_budget import leer_saldo_estimado_ia, verificar_saldo_ia
from app.repositories.ai_calls import LlamadaIARow, save_llamada_ia
from app.repositories.alertas import list_alertas

D = Decimal
MINIMO = D("0.60")


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _ayer() -> datetime:
    return _ahora() - timedelta(days=1)


def _gastar(fabrica, costo: str) -> None:
    with session_scope(fabrica) as s:
        save_llamada_ia(
            s,
            LlamadaIARow(
                agente="consolidador", modelo="m", prompt_version="v1", input_tokens=1, output_tokens=1,
                cost_usd=D(costo), respuesta={"ok": True}, exitosa=True, error_message=None,
            ),
        )


def _avisos(fabrica) -> list:
    with session_scope(fabrica) as s:
        return [a for a in list_alertas(s, limit=100) if a.tipo == TipoAlerta.SALDO_IA_INSUFICIENTE]


def _verificar(fabrica, *, saldo, en, now=None) -> bool:
    return verificar_saldo_ia(
        fabrica, saldo_declarado_usd=saldo, saldo_declarado_en=en, minimo_por_ciclo_usd=MINIMO, now=now or _ahora()
    )


def test_sin_saldo_declarado_la_guardia_no_actua_ni_inventa_un_numero(reinicios):
    assert _verificar(reinicios, saldo=None, en=None) is True
    assert _verificar(reinicios, saldo=D("0.10"), en=None) is True  # falta la fecha
    assert _verificar(reinicios, saldo=None, en=_ayer()) is True  # falta el saldo
    assert _avisos(reinicios) == []


def test_saldo_suficiente_deja_iniciar_sin_aviso(reinicios):
    assert _verificar(reinicios, saldo=D("5.50"), en=_ayer()) is True
    assert _avisos(reinicios) == []


def test_el_saldo_estimado_resta_lo_gastado_desde_la_declaracion(reinicios):
    _gastar(reinicios, "4.90")
    with session_scope(reinicios) as s:
        assert leer_saldo_estimado_ia(s, saldo_declarado_usd=D("5.50"), saldo_declarado_en=_ayer()) == D("0.60")


def test_exactamente_en_el_minimo_todavia_inicia(reinicios):
    _gastar(reinicios, "4.90")
    assert _verificar(reinicios, saldo=D("5.50"), en=_ayer()) is True
    assert _avisos(reinicios) == []


def test_un_centavo_bajo_el_minimo_no_inicia_y_deja_un_aviso(reinicios):
    _gastar(reinicios, "4.91")
    assert _verificar(reinicios, saldo=D("5.50"), en=_ayer()) is False
    [aviso] = _avisos(reinicios)
    assert aviso.observed_value == D("0.59") and aviso.threshold == MINIMO
    assert "SALDO DE IA INSUFICIENTE" in aviso.message


def test_el_caso_real_034_usd_no_inicia(reinicios):
    assert _verificar(reinicios, saldo=D("0.34"), en=_ayer()) is False
    assert len(_avisos(reinicios)) == 1


def test_el_gasto_anterior_a_la_declaracion_no_cuenta(reinicios):
    _gastar(reinicios, "9.00")
    futura = _ahora() + timedelta(hours=1)
    assert _verificar(reinicios, saldo=D("0.80"), en=futura) is True


def test_el_aviso_es_uno_por_dia_aunque_se_intente_varias_veces(reinicios):
    for _ in range(3):
        assert _verificar(reinicios, saldo=D("0.34"), en=_ayer()) is False
    assert len(_avisos(reinicios)) == 1


def test_al_dia_siguiente_vuelve_a_avisar(reinicios):
    _verificar(reinicios, saldo=D("0.34"), en=_ayer())
    _verificar(reinicios, saldo=D("0.34"), en=_ayer(), now=_ahora() + timedelta(days=1))
    assert len(_avisos(reinicios)) == 2


def test_recargar_y_actualizar_el_saldo_vuelve_a_permitir_ciclos(reinicios):
    assert _verificar(reinicios, saldo=D("0.34"), en=_ayer()) is False
    assert _verificar(reinicios, saldo=D("10.00"), en=_ahora() - timedelta(minutes=1)) is True


def test_si_no_se_puede_leer_el_gasto_falla_cerrado():
    class FabricaRota:
        def __call__(self, *a, **k):
            raise RuntimeError("base caída")

    assert _verificar(FabricaRota(), saldo=D("5.50"), en=_ayer()) is False
