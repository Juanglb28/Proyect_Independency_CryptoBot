"""Texto del resumen por Telegram de un ciclo (`app/domain/rules/cycle_summary.py`)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.rules.cycle_summary import (
    DatosResumenCiclo,
    OrigenCiclo,
    PropuestaParaResumen,
    mensaje_ciclo_fallido,
    mensaje_resumen_ciclo,
)

D = Decimal
AHORA = datetime(2026, 10, 11, 0, 12, tzinfo=timezone.utc)
CICLO = uuid.UUID("8608a24c-1964-4902-8b78-cb85798f378c")


def _datos(**cambios) -> DatosResumenCiclo:
    base = dict(
        origen=OrigenCiclo.PROGRAMADO_DIARIO,
        ciclo_id=CICLO,
        terminado_en=AHORA,
        candidatos_evaluados=("SOLUSDT", "ETHUSDT"),
        candidatos_excluidos_por_calidad=(),
        propuestas=(),
        costo_ciclo_usd=D("0.8421"),
        gasto_hoy_usd=D("0.8421"),
        tope_usd=D("2.00"),
        recomendacion="no_invertir",
        razon_no_invertir="Sin confirmación de volumen.",
    )
    base.update(cambios)
    return DatosResumenCiclo(**base)


def _propuesta(symbol="SOLUSDT", puntaje="58.2", tier="NORMAL", confianza="media") -> PropuestaParaResumen:
    return PropuestaParaResumen(symbol, D(puntaje), tier, confianza)


def test_encabezado_con_origen_hora_utc_y_ciclo_corto():
    texto = mensaje_resumen_ciclo(_datos())
    assert texto.startswith("RESUMEN DEL CICLO (programado 00:05 UTC)")
    assert "2026-10-11 00:12 UTC" in texto and "ciclo 8608a24c" in texto


@pytest.mark.parametrize(
    ("origen", "etiqueta"),
    [
        (OrigenCiclo.PROGRAMADO_DIARIO, "programado 00:05 UTC"),
        (OrigenCiclo.CANDIDATOS_NUEVOS, "disparado por candidatos nuevos"),
        (OrigenCiclo.MANUAL, "manual"),
    ],
)
def test_cada_origen_tiene_su_etiqueta(origen, etiqueta):
    assert f"({etiqueta})" in mensaje_resumen_ciclo(_datos(origen=origen))


def test_lista_los_candidatos_evaluados_con_su_cantidad():
    assert "Candidatos evaluados (2): SOLUSDT, ETHUSDT" in mensaje_resumen_ciclo(_datos())


def test_sin_candidatos_lo_dice_y_explica_el_prefiltro():
    texto = mensaje_resumen_ciclo(_datos(candidatos_evaluados=(), recomendacion=None, razon_no_invertir=None))
    assert "Candidatos evaluados: ninguno" in texto
    assert "El pre-filtro no dejó ningún candidato." in texto


def test_todos_excluidos_por_calidad_se_explica_y_no_se_dice_que_hubo_consolidador():
    texto = mensaje_resumen_ciclo(
        _datos(candidatos_excluidos_por_calidad=("SOLUSDT", "ETHUSDT"), recomendacion=None, razon_no_invertir=None)
    )
    assert "Excluidos por calidad de datos: SOLUSDT, ETHUSDT" in texto
    assert "no se llamó al Consolidador" in texto


def test_no_invertir_cita_la_razon_del_consolidador():
    texto = mensaje_resumen_ciclo(_datos())
    assert "Sin propuestas." in texto
    assert "El Consolidador recomendó NO invertir." in texto and "«Sin confirmación de volumen.»" in texto


def test_la_razon_larga_se_recorta_y_se_aplana():
    texto = mensaje_resumen_ciclo(_datos(razon_no_invertir="palabra\n" * 200))
    razon = texto.split("«")[1].split("»")[0]
    assert len(razon) <= 300 and "\n" not in razon and razon.endswith("…")


def test_recomendacion_inesperada_sin_propuesta_no_inventa_una_causa():
    texto = mensaje_resumen_ciclo(_datos(recomendacion="invertir", razon_no_invertir=None))
    assert "'invertir' pero no se guardó ninguna propuesta" in texto


def test_con_propuestas_lista_simbolo_puntaje_tier_y_confianza():
    texto = mensaje_resumen_ciclo(
        _datos(
            propuestas=(
                _propuesta(),
                _propuesta("ADAUSDT", "52.00", "MAYOR_RIESGO", "baja"),
                _propuesta("XRPUSDT", "40.5", "NINGUNO", "alta"),
            )
        )
    )
    assert "Propuestas (3):" in texto and "Sin propuestas" not in texto
    assert "• SOLUSDT: puntaje 58.20, supera el umbral de entrada, confianza media" in texto
    assert "• ADAUSDT: puntaje 52.00, entrada de MAYOR RIESGO (requiere tu aprobación), confianza baja" in texto
    assert "• XRPUSDT: puntaje 40.50, no califica para entrada, confianza alta" in texto


def test_costo_y_gasto_del_dia_frente_al_tope_con_dos_decimales():
    texto = mensaje_resumen_ciclo(_datos(costo_ciclo_usd=D("0.8421"), gasto_hoy_usd=D("1.2"), tope_usd=D("2")))
    assert "Costo del ciclo: 0.84 USD" in texto
    assert "Gasto de IA hoy (UTC): 1.20 de 2.00 USD" in texto


def test_cabe_en_un_mensaje_de_telegram_en_el_peor_caso():
    cinco = tuple(_propuesta(f"MON{i}USDT", "55.55", "MAYOR_RIESGO", "media") for i in range(5))
    texto = mensaje_resumen_ciclo(
        _datos(candidatos_evaluados=tuple(f"MON{i}USDT" for i in range(5)), propuestas=cinco)
    )
    assert len(texto) < 1500


def test_fecha_sin_zona_horaria_se_rechaza():
    with pytest.raises(ValueError):
        mensaje_resumen_ciclo(_datos(terminado_en=datetime(2026, 10, 11, 0, 12)))


def test_fecha_en_otra_zona_horaria_se_rechaza():
    with pytest.raises(ValueError):
        mensaje_resumen_ciclo(_datos(terminado_en=AHORA.astimezone(timezone(timedelta(hours=-5)))))


def test_ciclo_fallido_solo_lleva_el_nombre_del_error():
    texto = mensaje_ciclo_fallido(OrigenCiclo.MANUAL, ocurrido_en=AHORA, tipo_error="OperationalError")
    assert texto.startswith("CICLO FALLIDO (manual)")
    assert "OperationalError" in texto and "2026-10-11 00:12 UTC" in texto
    assert "monitor y los stops siguen funcionando" in texto


def test_ciclo_fallido_exige_utc():
    with pytest.raises(ValueError):
        mensaje_ciclo_fallido(OrigenCiclo.MANUAL, ocurrido_en=datetime(2026, 10, 11), tipo_error="X")


def test_sin_saldo_declarado_el_resumen_no_menciona_saldo():
    assert "Saldo estimado" not in mensaje_resumen_ciclo(_datos())


def test_saldo_suficiente_se_muestra_como_estimacion_sin_advertencia():
    texto = mensaje_resumen_ciclo(_datos(saldo_estimado_usd=D("3.2"), saldo_minimo_por_ciclo_usd=D("0.60")))
    assert "Saldo estimado de Anthropic: 3.20 USD (estimación, no la cifra de la consola)" in texto
    assert "NO alcanza" not in texto


def test_saldo_bajo_advierte_que_no_alcanza_para_otro_ciclo():
    texto = mensaje_resumen_ciclo(_datos(saldo_estimado_usd=D("0.34"), saldo_minimo_por_ciclo_usd=D("0.60")))
    assert "Saldo estimado de Anthropic: 0.34 USD" in texto
    assert "NO alcanza para otro ciclo automático (mínimo 0.60 USD)" in texto


def test_saldo_exactamente_en_el_minimo_no_advierte():
    texto = mensaje_resumen_ciclo(_datos(saldo_estimado_usd=D("0.60"), saldo_minimo_por_ciclo_usd=D("0.60")))
    assert "NO alcanza" not in texto


# --- posiciones paper abiertas automáticamente (decisión del 2026-10-12) -----------------


def _entrada(symbol="ETHUSDT", serie="OBSERVACION", puntaje="52.5", resultado="ABIERTA", motivos=()):
    from app.domain.rules.cycle_summary import EntradaParaResumen

    return EntradaParaResumen(symbol, serie, D(puntaje), resultado, tuple(motivos))


def test_sin_informar_la_apertura_el_resumen_no_dice_nada_de_posiciones():
    assert "Posiciones paper" not in mensaje_resumen_ciclo(_datos())


def test_apertura_informada_sin_candidatos_lo_dice():
    texto = mensaje_resumen_ciclo(_datos(entradas_informadas=True))
    assert "Posiciones paper: ningún candidato para abrir en este ciclo." in texto


def test_lista_cada_entrada_con_su_serie_y_su_resultado():
    texto = mensaje_resumen_ciclo(
        _datos(
            entradas_informadas=True,
            entradas=(
                _entrada("SOLUSDT", "ESTRATEGIA", "58.2", "PENDIENTE_APROBACION"),
                _entrada("ETHUSDT", "OBSERVACION", "52.5", "ABIERTA"),
                _entrada("BTCUSDT", "OBSERVACION", "47", "DESCARTADA", ("MAXIMO_OBSERVACIONES_DIA",)),
                _entrada("ADAUSDT", "OBSERVACION", "46", "SIN_PRECIO"),
            ),
        )
    )
    assert "• SOLUSDT [estrategia] puntaje 58.20: pendiente de tu aprobación" in texto
    assert "• ETHUSDT [observación] puntaje 52.50: posición paper ABIERTA" in texto
    assert "• BTCUSDT [observación] puntaje 47.00: no se abrió (MAXIMO_OBSERVACIONES_DIA)" in texto
    assert "• ADAUSDT [observación] puntaje 46.00: sin precio disponible, no se abrió" in texto


def test_una_apertura_fallida_se_avisa_y_no_se_listan_entradas():
    texto = mensaje_resumen_ciclo(_datos(apertura_fallo=True, entradas_informadas=False))
    assert "la apertura automática FALLÓ" in texto and "no se abrió nada" in texto


def test_una_apertura_bloqueada_por_reconciliacion_se_avisa():
    texto = mensaje_resumen_ciclo(_datos(apertura_bloqueada_por_reconciliacion=True))
    assert "BLOQUEADA" in texto and "reconciliación" in texto


def test_el_peor_caso_con_diez_entradas_sigue_cabiendo_en_telegram():
    entradas = tuple(
        _entrada(f"{s}USDT", "ESTRATEGIA" if i < 5 else "OBSERVACION", "50", "DESCARTADA", ("MAXIMO_OBSERVACIONES_DIA", "SIMBOLO_YA_ABIERTO"))
        for i, s in enumerate(["BTC", "ETH", "SOL", "ADA", "XRP", "DOT", "LINK", "AVAX", "NEAR", "AAVE"])
    )
    assert len(mensaje_resumen_ciclo(_datos(entradas_informadas=True, entradas=entradas))) < 4000
