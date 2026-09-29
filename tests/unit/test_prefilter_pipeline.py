"""Tests unitarios de app.data.prefilter_pipeline.

Usan `get_candles`/`get_indicators` parcheados en vez de Postgres real, así
que cubren solo la lógica de orquestación (a qué símbolo le falta qué dato,
cómo se arma `sma_pct_values`, qué termina en `symbols_sin_datos`) — no la
persistencia real, porque este pipeline todavía no persiste nada (las
tablas `factores`/`propuestas` son de un paso posterior).
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.data.exchange_client import Candle
from app.data.indicator_calculator import (
    INDICATOR_NAME_DISTANCIA_MAX_90D,
    INDICATOR_NAME_VOLUMEN_RELATIVO,
    IndicatorRow,
    sma_indicator_name,
)
from app.data.prefilter_pipeline import run_prefilter
from app.domain.symbols import ALLOWED_SYMBOLS

_T = datetime(2026, 9, 29, tzinfo=timezone.utc)


def _candle(symbol: str, close: Decimal) -> Candle:
    return Candle(
        symbol=symbol,
        timeframe="1d",
        open_time=_T,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=Decimal("100"),
    )


def _full_indicators(symbol: str, close: Decimal) -> list[IndicatorRow]:
    """Indicadores completos para que el símbolo dé exactamente
    tendencia=0, volumen=1.0, distancia=-10 (score_final=50, ver
    test_prefilter.py::test_calculate_prefilter_score_combines_three_factors_with_equal_weights)."""
    rows = [
        IndicatorRow(symbol, "1d", _T, sma_indicator_name(90), close),
        IndicatorRow(symbol, "1d", _T, sma_indicator_name(120), close),
        IndicatorRow(symbol, "1d", _T, sma_indicator_name(180), close),
        IndicatorRow(symbol, "1d", _T, INDICATOR_NAME_VOLUMEN_RELATIVO, Decimal("1.0")),
        IndicatorRow(symbol, "1d", _T, INDICATOR_NAME_DISTANCIA_MAX_90D, Decimal("-10")),
    ]
    return rows


def _patch_repositories(monkeypatch, candles_by_symbol, indicators_by_symbol):
    def fake_get_candles(session, symbol, timeframe):
        return candles_by_symbol.get(symbol, [])

    def fake_get_indicators(session, symbol, timeframe):
        return indicators_by_symbol.get(symbol, [])

    monkeypatch.setattr("app.data.prefilter_pipeline.get_candles", fake_get_candles)
    monkeypatch.setattr("app.data.prefilter_pipeline.get_indicators", fake_get_indicators)


def test_run_prefilter_scores_symbol_with_complete_data(monkeypatch):
    close = Decimal("100")
    candles_by_symbol = {"BTCUSDT": [_candle("BTCUSDT", close)]}
    indicators_by_symbol = {"BTCUSDT": _full_indicators("BTCUSDT", close)}
    _patch_repositories(monkeypatch, candles_by_symbol, indicators_by_symbol)

    result = run_prefilter(session=object())

    scored_symbols = {s.symbol for s in result.scores}
    assert "BTCUSDT" in scored_symbols
    btc_score = next(s for s in result.scores if s.symbol == "BTCUSDT")
    assert btc_score.score_final == Decimal(50)

    # Todos los demás símbolos de la whitelist, sin velas, deben quedar
    # declarados sin datos, nunca silenciosamente ignorados.
    assert set(result.symbols_sin_datos) == set(ALLOWED_SYMBOLS) - {"BTCUSDT"}


def test_run_prefilter_declares_missing_data_when_symbol_has_no_candles(monkeypatch):
    _patch_repositories(monkeypatch, {}, {})

    result = run_prefilter(session=object())

    assert result.scores == ()
    assert result.candidates == ()
    assert set(result.symbols_sin_datos) == set(ALLOWED_SYMBOLS)


def test_run_prefilter_declares_missing_data_when_an_indicator_is_absent(monkeypatch):
    close = Decimal("100")
    candles_by_symbol = {"BTCUSDT": [_candle("BTCUSDT", close)]}
    # Faltan los indicadores por completo (p. ej. Paso 4 no se corrió
    # todavía para este símbolo) -> no debe inventarse un puntaje.
    indicators_by_symbol = {"BTCUSDT": []}
    _patch_repositories(monkeypatch, candles_by_symbol, indicators_by_symbol)

    result = run_prefilter(session=object())

    assert result.scores == ()
    assert "BTCUSDT" in result.symbols_sin_datos


def test_run_prefilter_does_not_use_a_stale_indicator_from_a_different_candle(monkeypatch):
    """Si el indicador guardado corresponde a una vela ANTERIOR a la
    última vela disponible (p. ej. Paso 4 no se volvió a correr tras una
    nueva vela), no debe mezclarse con el cierre más reciente: eso sería
    exactamente el error de trazabilidad que la sección 15 del plan
    prohíbe (look-ahead / desalineación de instantes)."""
    close = Decimal("100")
    stale_time = datetime(2026, 9, 28, tzinfo=timezone.utc)
    candles_by_symbol = {"BTCUSDT": [_candle("BTCUSDT", close)]}  # open_time = _T (29 sept)
    indicators_by_symbol = {
        "BTCUSDT": [
            IndicatorRow("BTCUSDT", "1d", stale_time, sma_indicator_name(90), close),
            IndicatorRow("BTCUSDT", "1d", stale_time, sma_indicator_name(120), close),
            IndicatorRow("BTCUSDT", "1d", stale_time, sma_indicator_name(180), close),
            IndicatorRow("BTCUSDT", "1d", stale_time, INDICATOR_NAME_VOLUMEN_RELATIVO, Decimal("1.0")),
            IndicatorRow(
                "BTCUSDT", "1d", stale_time, INDICATOR_NAME_DISTANCIA_MAX_90D, Decimal("-10")
            ),
        ]
    }
    _patch_repositories(monkeypatch, candles_by_symbol, indicators_by_symbol)

    result = run_prefilter(session=object())

    assert result.scores == ()
    assert "BTCUSDT" in result.symbols_sin_datos


def test_run_prefilter_selects_candidates_from_scores(monkeypatch):
    # BTCUSDT con score alto (tendencia/volumen/distancia todos al máximo);
    # ETHUSDT con score bajo (todos al mínimo) -> solo BTCUSDT candidato.
    high_close = Decimal("125")  # (125-100)/100*100 = 25 = límite superior
    low_close = Decimal("75")  # (75-100)/100*100 = -25 = límite inferior
    sma_ref = Decimal("100")

    def indicators_extreme(symbol, close, volumen, distancia):
        return [
            IndicatorRow(symbol, "1d", _T, sma_indicator_name(90), sma_ref),
            IndicatorRow(symbol, "1d", _T, sma_indicator_name(120), sma_ref),
            IndicatorRow(symbol, "1d", _T, sma_indicator_name(180), sma_ref),
            IndicatorRow(symbol, "1d", _T, INDICATOR_NAME_VOLUMEN_RELATIVO, volumen),
            IndicatorRow(symbol, "1d", _T, INDICATOR_NAME_DISTANCIA_MAX_90D, distancia),
        ]

    candles_by_symbol = {
        "BTCUSDT": [_candle("BTCUSDT", high_close)],
        "ETHUSDT": [_candle("ETHUSDT", low_close)],
    }
    indicators_by_symbol = {
        "BTCUSDT": indicators_extreme("BTCUSDT", high_close, Decimal("1.6"), Decimal("0")),
        "ETHUSDT": indicators_extreme("ETHUSDT", low_close, Decimal("0.4"), Decimal("-20")),
    }
    _patch_repositories(monkeypatch, candles_by_symbol, indicators_by_symbol)

    result = run_prefilter(session=object())

    assert [c.symbol for c in result.candidates] == ["BTCUSDT"]
