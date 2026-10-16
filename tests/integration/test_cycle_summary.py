"""Resumen y aviso de fallo de un ciclo (`app/orchestration/cycle_summary.py`) contra Postgres real,
más el envío por el despachador (con un canal falso: nunca se contacta a Telegram)."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from app.db.session import session_scope
from app.domain.enums import EstadoCiclo, TierEntrada, TipoAlerta
from app.domain.rules.cycle_summary import OrigenCiclo
from app.notifications.dispatcher import dispatch_pending_alerts
from app.orchestration.cycle_summary import registrar_ciclo_fallido, registrar_resumen_ciclo
from app.orchestration.daily_cycle import DailyCycleResult
from app.repositories.ai_calls import LlamadaIARow, save_llamada_ia
from app.repositories.alertas import list_alertas
from app.repositories.analysis import create_ciclo, finalizar_ciclo, save_propuesta
from tests.integration.test_analysis_repository import _propuesta_row

D = Decimal
TOPE = D("2.00")


class CanalFalso:
    def __init__(self):
        self.enviados: list[str] = []

    def send(self, text: str) -> None:
        self.enviados.append(text)


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _gastar(fabrica, costo: str) -> None:
    with session_scope(fabrica) as s:
        save_llamada_ia(
            s,
            LlamadaIARow(
                agente="consolidador", modelo="m", prompt_version="v1", input_tokens=1, output_tokens=1,
                cost_usd=D(costo), respuesta={"ok": True}, exitosa=True, error_message=None,
            ),
        )


def _ciclo_con(fabrica, *, propuestas: list[tuple[str, TierEntrada, str]], candidatos: tuple[str, ...]) -> DailyCycleResult:
    with session_scope(fabrica) as s:
        ciclo = create_ciclo(s, started_at=_ahora(), status=EstadoCiclo.PREFILTER)
        finalizar_ciclo(
            s, ciclo, status=EstadoCiclo.FREEZE_DECISION_SNAPSHOT, finished_at=_ahora(), cost_usd=D("0.50"),
            error_message=None,
        )
        ids = []
        for symbol, tier, puntaje in propuestas:
            row = dataclasses.replace(_propuesta_row(ciclo, symbol), tier_entrada=tier, puntaje_ponderado=D(puntaje))
            ids.append(save_propuesta(s, row))
    return DailyCycleResult(
        ciclo_id=ciclo,
        candidatos_evaluados=candidatos,
        candidatos_excluidos_por_calidad=(),
        propuesta_ids=tuple(ids),
        cost_usd=D("0.50"),
        consolidador_recomendacion="invertir" if ids else "no_invertir",
        consolidador_razon_no_invertir=None if ids else "Mercado lateral sin confirmación.",
    )


def _avisos(fabrica, tipo: TipoAlerta) -> list:
    with session_scope(fabrica) as s:
        return [a for a in list_alertas(s, limit=100) if a.tipo == tipo]


def test_con_propuestas_guarda_un_resumen_con_sus_datos_reales(reinicios):
    _gastar(reinicios, "0.50")
    resultado = _ciclo_con(
        reinicios,
        propuestas=[("SOLUSDT", TierEntrada.NORMAL, "58.20"), ("ADAUSDT", TierEntrada.MAYOR_RIESGO, "52.00")],
        candidatos=("SOLUSDT", "ADAUSDT", "XRPUSDT"),
    )

    assert registrar_resumen_ciclo(
        reinicios, resultado, origen=OrigenCiclo.PROGRAMADO_DIARIO, tope_usd=TOPE, now=_ahora()
    ) is True

    [aviso] = _avisos(reinicios, TipoAlerta.RESUMEN_CICLO)
    assert aviso.dedup_key == f"resumen-ciclo:{resultado.ciclo_id}"
    assert "Candidatos evaluados (3): SOLUSDT, ADAUSDT, XRPUSDT" in aviso.message
    assert "• SOLUSDT: puntaje 58.20, supera el umbral de entrada" in aviso.message
    assert "• ADAUSDT: puntaje 52.00, entrada de MAYOR RIESGO" in aviso.message
    assert "Gasto de IA hoy (UTC): 0.50 de 2.00 USD" in aviso.message
    assert aviso.observed_value == D("0.50") and aviso.threshold == TOPE


def test_sin_propuestas_explica_el_motivo(reinicios):
    resultado = _ciclo_con(reinicios, propuestas=[], candidatos=("SOLUSDT",))
    registrar_resumen_ciclo(reinicios, resultado, origen=OrigenCiclo.MANUAL, tope_usd=TOPE, now=_ahora())

    [aviso] = _avisos(reinicios, TipoAlerta.RESUMEN_CICLO)
    assert "(manual)" in aviso.message
    assert "Sin propuestas." in aviso.message and "Mercado lateral sin confirmación." in aviso.message


def test_registrar_dos_veces_el_mismo_ciclo_no_duplica_el_resumen(reinicios):
    resultado = _ciclo_con(reinicios, propuestas=[], candidatos=())
    primero = registrar_resumen_ciclo(reinicios, resultado, origen=OrigenCiclo.MANUAL, tope_usd=TOPE, now=_ahora())
    segundo = registrar_resumen_ciclo(reinicios, resultado, origen=OrigenCiclo.MANUAL, tope_usd=TOPE, now=_ahora())

    assert (primero, segundo) == (True, False)
    assert len(_avisos(reinicios, TipoAlerta.RESUMEN_CICLO)) == 1


def test_dos_ciclos_distintos_dejan_dos_resumenes(reinicios):
    for _ in range(2):
        resultado = _ciclo_con(reinicios, propuestas=[], candidatos=())
        registrar_resumen_ciclo(reinicios, resultado, origen=OrigenCiclo.MANUAL, tope_usd=TOPE, now=_ahora())

    assert len(_avisos(reinicios, TipoAlerta.RESUMEN_CICLO)) == 2


def test_un_ciclo_que_no_existe_no_lanza_error_ni_deja_aviso(reinicios):
    fantasma = DailyCycleResult(
        ciclo_id=uuid.uuid4(), candidatos_evaluados=(), candidatos_excluidos_por_calidad=(),
        propuesta_ids=(), cost_usd=D("0"),
    )
    # Sin propuestas en la base el resumen igual se arma; lo importante: nunca propaga un error.
    registrar_resumen_ciclo(reinicios, fantasma, origen=OrigenCiclo.MANUAL, tope_usd=TOPE, now=_ahora())


def test_si_la_base_falla_el_registro_devuelve_false_sin_propagar():
    class FabricaRota:
        def __call__(self, *a, **k):
            raise RuntimeError("base caída")

    resultado = DailyCycleResult(
        ciclo_id=uuid.uuid4(), candidatos_evaluados=(), candidatos_excluidos_por_calidad=(),
        propuesta_ids=(), cost_usd=D("0"),
    )
    assert registrar_resumen_ciclo(FabricaRota(), resultado, origen=OrigenCiclo.MANUAL, tope_usd=TOPE, now=_ahora()) is False
    assert registrar_ciclo_fallido(FabricaRota(), origen=OrigenCiclo.MANUAL, error=ValueError("x"), now=_ahora()) is False


def test_ciclo_fallido_guarda_solo_el_nombre_del_error(reinicios):
    error = RuntimeError("postgresql://crypto:SECRETO@host/db no responde")

    assert registrar_ciclo_fallido(reinicios, origen=OrigenCiclo.CANDIDATOS_NUEVOS, error=error, now=_ahora()) is True

    [aviso] = _avisos(reinicios, TipoAlerta.CICLO_FALLIDO)
    assert "RuntimeError" in aviso.message and "disparado por candidatos nuevos" in aviso.message
    assert "SECRETO" not in aviso.message and "postgresql" not in aviso.message


def test_dos_fallos_en_instantes_distintos_dejan_dos_avisos(reinicios):
    base = _ahora()
    registrar_ciclo_fallido(reinicios, origen=OrigenCiclo.MANUAL, error=ValueError("a"), now=base)
    registrar_ciclo_fallido(reinicios, origen=OrigenCiclo.MANUAL, error=ValueError("b"), now=base.replace(microsecond=(base.microsecond + 1) % 1_000_000))
    assert len(_avisos(reinicios, TipoAlerta.CICLO_FALLIDO)) == 2


def test_el_despachador_envia_el_resumen_una_sola_vez(reinicios):
    resultado = _ciclo_con(reinicios, propuestas=[("SOLUSDT", TierEntrada.NORMAL, "58.20")], candidatos=("SOLUSDT",))
    registrar_resumen_ciclo(reinicios, resultado, origen=OrigenCiclo.PROGRAMADO_DIARIO, tope_usd=TOPE, now=_ahora())
    canal = CanalFalso()

    primero = dispatch_pending_alerts(reinicios, canal, now=_ahora())
    segundo = dispatch_pending_alerts(reinicios, canal, now=_ahora())

    assert primero.enviadas >= 1 and segundo.enviadas == 0
    resumenes = [t for t in canal.enviados if t.startswith("RESUMEN DEL CICLO")]
    assert len(resumenes) == 1 and "SOLUSDT" in resumenes[0]


def test_el_resumen_incluye_el_saldo_estimado_cuando_esta_declarado(reinicios):
    from datetime import timedelta

    _gastar(reinicios, "0.50")
    resultado = _ciclo_con(reinicios, propuestas=[], candidatos=())
    registrar_resumen_ciclo(
        reinicios, resultado, origen=OrigenCiclo.MANUAL, tope_usd=TOPE, now=_ahora(),
        saldo_declarado_usd=D("0.84"), saldo_declarado_en=_ahora() - timedelta(days=1),
        saldo_minimo_por_ciclo_usd=D("0.60"),
    )

    [aviso] = _avisos(reinicios, TipoAlerta.RESUMEN_CICLO)
    assert "Saldo estimado de Anthropic: 0.34 USD" in aviso.message
    assert "NO alcanza para otro ciclo automático" in aviso.message


def test_sin_saldo_declarado_el_resumen_no_trae_linea_de_saldo(reinicios):
    resultado = _ciclo_con(reinicios, propuestas=[], candidatos=())
    registrar_resumen_ciclo(reinicios, resultado, origen=OrigenCiclo.MANUAL, tope_usd=TOPE, now=_ahora())

    [aviso] = _avisos(reinicios, TipoAlerta.RESUMEN_CICLO)
    assert "Saldo estimado" not in aviso.message
