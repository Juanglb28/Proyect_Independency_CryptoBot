"""Tope diario de gasto de IA (`app/orchestration/ai_budget.py`) contra Postgres real.

`llamadas_ia.created_at` lo pone la base (`now()`), así que "hoy" es el día real
y "mañana" se simula adelantando el `now` que se le pasa al guardia.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.db.session import session_scope
from app.domain.enums import TipoAlerta
from app.orchestration.ai_budget import (
    avisar_presupuesto_ia_agotado,
    leer_presupuesto_ia,
    verificar_presupuesto_ia,
)
from app.repositories.ai_calls import LlamadaIARow, save_llamada_ia
from app.repositories.alertas import list_alertas
from app.services.system import get_system_view

D = Decimal
TOPE = D("2.00")


def _hoy() -> datetime:
    return datetime.now(timezone.utc)


def _gastar(fabrica, costo: str, *, exitosa: bool = True) -> None:
    with session_scope(fabrica) as s:
        save_llamada_ia(
            s,
            LlamadaIARow(
                agente="analista_cuantitativo", modelo="m", prompt_version="v1", input_tokens=1, output_tokens=1,
                cost_usd=D(costo), respuesta={"ok": True} if exitosa else None, exitosa=exitosa,
                error_message=None if exitosa else "json invalido",
            ),
        )


def _avisos(fabrica) -> list:
    with session_scope(fabrica) as s:
        return [a for a in list_alertas(s, limit=50) if a.tipo == TipoAlerta.PRESUPUESTO_IA_AGOTADO]


def test_sin_gasto_el_ciclo_puede_iniciar_y_no_hay_aviso(reinicios):
    assert verificar_presupuesto_ia(reinicios, tope_usd=TOPE, now=_hoy()) is True
    assert _avisos(reinicios) == []


def test_un_centavo_antes_del_tope_todavia_permite_iniciar(reinicios):
    _gastar(reinicios, "1.99")
    assert verificar_presupuesto_ia(reinicios, tope_usd=TOPE, now=_hoy()) is True
    assert _avisos(reinicios) == []


def test_exactamente_en_el_tope_no_inicia_y_deja_un_aviso(reinicios):
    _gastar(reinicios, "1.25")
    _gastar(reinicios, "0.75")
    assert verificar_presupuesto_ia(reinicios, tope_usd=TOPE, now=_hoy()) is False
    avisos = _avisos(reinicios)
    assert len(avisos) == 1
    assert avisos[0].observed_value == D("2.00") and avisos[0].threshold == TOPE
    assert "PRESUPUESTO DE IA AGOTADO" in avisos[0].message


def test_el_aviso_es_uno_por_dia_aunque_se_intente_varias_veces(reinicios):
    _gastar(reinicios, "2.50")
    for _ in range(4):
        assert verificar_presupuesto_ia(reinicios, tope_usd=TOPE, now=_hoy()) is False
    assert len(_avisos(reinicios)) == 1


def test_las_llamadas_fallidas_tambien_cuentan_porque_se_cobran(reinicios):
    _gastar(reinicios, "1.00")
    _gastar(reinicios, "1.10", exitosa=False)
    assert verificar_presupuesto_ia(reinicios, tope_usd=TOPE, now=_hoy()) is False


def test_el_gasto_de_ayer_no_cuenta_para_hoy_y_el_aviso_de_ayer_no_bloquea_el_de_manana(reinicios):
    _gastar(reinicios, "3.00")
    assert verificar_presupuesto_ia(reinicios, tope_usd=TOPE, now=_hoy()) is False
    manana = _hoy() + timedelta(days=1)
    assert verificar_presupuesto_ia(reinicios, tope_usd=TOPE, now=manana) is True  # día nuevo, gasto 0
    _gastar(reinicios, "0.01")  # se registra con la fecha real: para "mañana" sigue sin contar
    assert verificar_presupuesto_ia(reinicios, tope_usd=TOPE, now=manana) is True
    pasado = manana + timedelta(days=1)
    assert verificar_presupuesto_ia(reinicios, tope_usd=TOPE, now=pasado) is True


def test_si_no_se_puede_leer_el_gasto_el_ciclo_no_inicia(reinicios):
    def fabrica_rota():
        raise RuntimeError("base caída")

    assert verificar_presupuesto_ia(fabrica_rota, tope_usd=TOPE, now=_hoy()) is False


def test_leer_presupuesto_devuelve_el_gasto_del_dia_utc(reinicios):
    _gastar(reinicios, "0.4900")
    with session_scope(reinicios) as s:
        estado = leer_presupuesto_ia(s, tope_usd=TOPE, now=_hoy())
        assert (estado.gasto_hoy_usd, estado.tope_usd, estado.agotado) == (D("0.4900"), TOPE, False)
        assert estado.dia == _hoy().date()


def test_avisar_directamente_es_idempotente(reinicios):
    _gastar(reinicios, "2.00")
    with session_scope(reinicios) as s:
        estado = leer_presupuesto_ia(s, tope_usd=TOPE, now=_hoy())
        assert avisar_presupuesto_ia_agotado(s, estado, now=_hoy()) is True
        assert avisar_presupuesto_ia_agotado(s, estado, now=_hoy()) is False


def test_la_vista_del_sistema_trae_el_gasto_de_hoy_frente_al_tope(db_session, tmp_path):
    save_llamada_ia(
        db_session,
        LlamadaIARow(agente="a", modelo="m", prompt_version="v1", input_tokens=1, output_tokens=1,
                     cost_usd=D("2.10"), respuesta={"ok": True}),
    )
    vista = get_system_view(
        db_session, initial_capital=D("1000"), now=_hoy(), heartbeat_path=tmp_path / "no-existe.json",
        tope_ia_diario_usd=TOPE,
    )
    assert vista.presupuesto_ia is not None
    assert vista.presupuesto_ia.gasto_hoy_usd == D("2.10") and vista.presupuesto_ia.agotado is True

    sin_tope = get_system_view(
        db_session, initial_capital=D("1000"), now=_hoy(), heartbeat_path=tmp_path / "no-existe.json"
    )
    assert sin_tope.presupuesto_ia is None
