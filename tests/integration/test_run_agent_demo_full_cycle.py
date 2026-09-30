"""Tests de las funciones que arman los bloques DATOS reales en
scripts/run_agent_demo_full_cycle.py — la parte con lógica real (leer la
base, detectar datos faltantes) del script de humo del Paso 6. El propio
`main()` (que hace 4 llamadas reales a la API de Anthropic) no se prueba
aquí, igual que `scripts/run_agent_demo.py` tampoco tiene test — ver su
docstring.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.data.exchange_client import Candle
from app.data.event_analyzer import EventAnalysisRow
from app.data.indicator_calculator import IndicatorRow, sma_indicator_name
from app.repositories.candles import save_candles
from app.repositories.event_analysis import save_event_analysis
from app.repositories.events import EventoInput, get_events, save_events
from app.repositories.indicators import save_indicators
from scripts.run_agent_demo_full_cycle import (
    _pick_symbol,
    build_datos_analista_cuantitativo,
    build_datos_analista_eventos,
    build_datos_auditor,
)


def _candle(symbol: str, timeframe: str, open_time: datetime, **overrides) -> Candle:
    base = Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time=open_time,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("95"),
        close=Decimal("105"),
        volume=Decimal("1000"),
    )
    base.update(overrides)
    return base


def _seed_full_daily_indicators(symbol: str, open_time: datetime) -> list[IndicatorRow]:
    rows = [
        IndicatorRow(symbol, "1d", open_time, "RSI_14", Decimal("62.5")),
        IndicatorRow(symbol, "1d", open_time, "ATR_14", Decimal("3.2")),
        IndicatorRow(symbol, "1d", open_time, "VOLUMEN_RELATIVO_30", Decimal("1.8")),
        IndicatorRow(symbol, "1d", open_time, "DISTANCIA_ATH", Decimal("-12.5")),
        IndicatorRow(symbol, "1d", open_time, "DISTANCIA_MAX_90D", Decimal("-4.1")),
    ]
    for period in (90, 120, 180):
        rows.append(IndicatorRow(symbol, "1d", open_time, sma_indicator_name(period), Decimal("100")))
    return rows


# ---------------------------------------------------------------------------
# _pick_symbol
# ---------------------------------------------------------------------------


def test_pick_symbol_returns_the_requested_symbol_when_valid(db_session):
    assert _pick_symbol(db_session, "AVAXUSDT") == "AVAXUSDT"


def test_pick_symbol_uppercases_the_requested_symbol(db_session):
    assert _pick_symbol(db_session, "avaxusdt") == "AVAXUSDT"


def test_pick_symbol_rejects_a_symbol_outside_the_whitelist(db_session):
    with pytest.raises(SystemExit):
        _pick_symbol(db_session, "DOGECOINUSDT")


# ---------------------------------------------------------------------------
# build_datos_analista_cuantitativo
# ---------------------------------------------------------------------------


def test_build_datos_analista_cuantitativo_with_full_daily_data_reports_no_missing_daily_fields(db_session):
    open_time = datetime(2026, 9, 1, tzinfo=timezone.utc)
    save_candles(db_session, [_candle("AVAXUSDT", "1d", open_time, close=Decimal("105"))])
    save_indicators(db_session, _seed_full_daily_indicators("AVAXUSDT", open_time))

    datos, faltantes = build_datos_analista_cuantitativo(db_session, "AVAXUSDT")

    assert datos["simbolo"] == "AVAXUSDT"
    bloque_1d = datos["temporalidades"]["1d"]
    assert bloque_1d["precio_cierre_ultima_vela"] == 105.0
    assert bloque_1d["rsi_14"] == 62.5
    assert bloque_1d["sma_90"] == 100.0
    assert bloque_1d["distancia_pct_sma_90"] == 5.0  # (105-100)/100*100
    assert bloque_1d["distancia_pct_ath"] == -12.5
    assert not any(f.endswith("_1d") or f in ("distancia_pct_ath", "distancia_pct_max_90d") for f in faltantes)


def test_build_datos_analista_cuantitativo_reports_missing_timeframe_with_no_candles(db_session):
    # Ninguna vela de ningún timeframe para este símbolo todavía.
    datos, faltantes = build_datos_analista_cuantitativo(db_session, "UNIUSDT")

    for timeframe in ("1h", "4h", "12h", "1d"):
        assert datos["temporalidades"][timeframe] == {"disponible": False}
        assert f"velas_{timeframe}" in faltantes


def test_build_datos_analista_cuantitativo_reports_missing_indicator_without_inventing_a_value(db_session):
    open_time = datetime(2026, 9, 1, tzinfo=timezone.utc)
    save_candles(db_session, [_candle("NEARUSDT", "1d", open_time)])
    # Solo se guarda RSI — el resto de indicadores diarios quedan sin calcular.
    save_indicators(db_session, [IndicatorRow("NEARUSDT", "1d", open_time, "RSI_14", Decimal("40"))])

    datos, faltantes = build_datos_analista_cuantitativo(db_session, "NEARUSDT")

    bloque_1d = datos["temporalidades"]["1d"]
    assert bloque_1d["rsi_14"] == 40.0
    assert bloque_1d["atr_14"] is None
    assert bloque_1d["sma_90"] is None
    assert "atr_14_1d" in faltantes
    assert "sma_90_1d" in faltantes
    assert "distancia_pct_ath" in faltantes


def test_build_datos_analista_cuantitativo_does_not_report_ath_distance_missing_on_non_daily_timeframes(
    db_session,
):
    # DISTANCIA_ATH/DISTANCIA_MAX_90D nunca se calculan fuera de 1d (sección
    # 6.1 del plan) — no es un dato faltante, es un indicador que no aplica.
    open_time = datetime(2026, 9, 1, tzinfo=timezone.utc)
    save_candles(db_session, [_candle("LTCUSDT", "1h", open_time)])
    save_indicators(db_session, [IndicatorRow("LTCUSDT", "1h", open_time, "RSI_14", Decimal("50"))])

    _, faltantes = build_datos_analista_cuantitativo(db_session, "LTCUSDT")

    assert "distancia_pct_ath" not in faltantes
    assert "distancia_pct_max_90d" not in faltantes


# ---------------------------------------------------------------------------
# build_datos_analista_eventos
# ---------------------------------------------------------------------------


def test_build_datos_analista_eventos_aggregates_windows_per_event(db_session):
    inserted = save_events(
        db_session,
        [
            EventoInput(
                external_id="evento-de-prueba",
                event_date=date(2024, 1, 1),
                event_type="REGULATORIO",
                description="descripción curada del evento",
                severity="alta",
                scope="ETHUSDT",
            )
        ],
    )
    assert inserted == 1
    evento = next(e for e in get_events(db_session) if e.external_id == "evento-de-prueba")

    save_event_analysis(
        db_session,
        [
            EventAnalysisRow(
                evento_id=evento.id,
                symbol="ETHUSDT",
                window_offset_days=offset,
                reference_date=date(2024, 1, 1),
                reference_price=Decimal("100"),
                window_date=date(2024, 1, 1),
                window_price=Decimal("110"),
                price_change_pct=Decimal("10"),
            )
            for offset in (-7, 1, 7)
        ],
    )

    datos, faltantes = build_datos_analista_eventos(db_session, "ETHUSDT")

    assert faltantes == []
    assert len(datos["eventos_del_catalogo_en_alcance"]) == 1
    entrada = datos["eventos_del_catalogo_en_alcance"][0]
    assert entrada["evento_id"] == "evento-de-prueba"
    assert entrada["descripcion"] == "descripción curada del evento"
    assert entrada["cambios_precio_pct_por_ventana_dias"] == {"-7": 10.0, "1": 10.0, "7": 10.0}


def test_build_datos_analista_eventos_reports_missing_when_symbol_has_no_analysis_rows(db_session):
    datos, faltantes = build_datos_analista_eventos(db_session, "SUIUSDT")

    assert datos["eventos_del_catalogo_en_alcance"] == []
    assert faltantes == ["analisis_eventos_para_este_simbolo"]


# ---------------------------------------------------------------------------
# build_datos_auditor
# ---------------------------------------------------------------------------


def test_build_datos_auditor_reflects_real_candle_recency_and_reported_gaps(db_session):
    open_time = datetime(2026, 9, 1, tzinfo=timezone.utc)
    save_candles(db_session, [_candle("ADAUSDT", "1d", open_time)])

    datos = build_datos_auditor(db_session, "ADAUSDT", ["atr_14_1h"], [], eventos_en_catalogo=2)

    assert datos["velas_diarias_disponibles"] == 1
    assert datos["ultima_vela_hace_horas"] > 0
    assert datos["campos_faltantes_reportados_por_analista_cuantitativo"] == ["atr_14_1h"]
    assert datos["eventos_del_catalogo_en_alcance_de_este_simbolo"] == 2


def test_build_datos_auditor_flags_a_gap_between_first_and_last_daily_candle(db_session):
    save_candles(
        db_session,
        [
            _candle("XRPUSDT", "1d", datetime(2026, 1, 1, tzinfo=timezone.utc)),
            # Faltan 8 días intermedios antes de esta segunda vela.
            _candle("XRPUSDT", "1d", datetime(2026, 1, 10, tzinfo=timezone.utc)),
        ],
    )

    datos = build_datos_auditor(db_session, "XRPUSDT", [], [], eventos_en_catalogo=0)

    assert datos["velas_diarias_disponibles"] == 2
    assert len(datos["huecos_en_series_detectados"]) == 1
    assert "posible hueco" in datos["huecos_en_series_detectados"][0]


def test_build_datos_auditor_with_no_candles_reports_none_recency_and_no_crash(db_session):
    datos = build_datos_auditor(db_session, "BCHUSDT", [], [], eventos_en_catalogo=0)

    assert datos["velas_diarias_disponibles"] == 0
    assert datos["ultima_vela_hace_horas"] is None
    assert datos["huecos_en_series_detectados"] == []
