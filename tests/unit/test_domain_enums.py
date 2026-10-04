"""Tests de app.domain.enums (Paso 7)."""

from app.domain.enums import EstadoCiclo


def test_estado_ciclo_tiene_exactamente_las_cinco_etapas_de_analisis():
    # Deliberadamente NO incluye INGEST/VALIDATE_DATA/CALCULATE_INDICATORS
    # (procesos separados y manuales, ver README) ni RISK_VALIDATION en
    # adelante (Paso 8/9) — ver el docstring de EstadoCiclo. Este test
    # falla a propósito si alguien agrega una etapa sin código real que la
    # transite (sección 30 del master prompt).
    assert [e.value for e in EstadoCiclo] == [
        "PREFILTER",
        "EVENT_CORRELATION",
        "AI_ANALYSIS",
        "CONSOLIDATE",
        "FREEZE_DECISION_SNAPSHOT",
    ]


def test_estado_ciclo_es_string_para_persistir_directo_como_texto():
    assert EstadoCiclo.PREFILTER == "PREFILTER"
    assert isinstance(EstadoCiclo.PREFILTER.value, str)


def test_intencion_orden_salidas_coinciden_con_exit_reason_del_plan():
    # Sección 8.4 del plan: valores controlados de `exit_reason`. Las
    # intenciones de salida deben ser EXACTAMENTE esos valores para que
    # `trade_results.exit_reason` salga directo de la orden de salida.
    from app.domain.enums import IntencionOrden

    assert [e.value for e in IntencionOrden if e != IntencionOrden.ENTRY] == [
        "STOP_LOSS",
        "TAKE_PROFIT_PARTIAL",
        "TRAILING_STOP",
        "MANUAL_APPROVAL_EXIT",
        "SYSTEM_DRAWDOWN",
        "RECONCILIATION_EXIT",
        "EMERGENCY_STOP",
        "OTHER",
    ]
    assert IntencionOrden.ENTRY.value == "ENTRY"


def test_estados_de_orden_y_posicion_son_los_aprobados_el_2026_10_04():
    from app.domain.enums import EstadoOrden, EstadoPosicion, LadoOrden

    assert [e.value for e in EstadoOrden] == ["PENDING_APPROVAL", "FILLED", "REJECTED", "EXPIRED"]
    assert [e.value for e in EstadoPosicion] == ["OPEN", "CLOSED"]
    assert [e.value for e in LadoOrden] == ["BUY", "SELL"]
