"""Tests unitarios de app.data.indicator_verification.

Usan `get_candles`/`get_indicators` parcheados en vez de Postgres real, así
que cubren solo la lógica de comparación y de invariantes — el round-trip
real contra la base de datos se prueba en
tests/integration/test_indicator_verification.py."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.data.exchange_client import Candle
from app.data.indicator_calculator import IndicatorRow, calculate_indicators
from app.data.indicator_verification import verify_all, verify_symbol_timeframe


def _candles(symbol: str, timeframe: str, n: int) -> list[Candle]:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    price = Decimal("100")
    out = []
    for i in range(n):
        price = price + Decimal(i % 5) - Decimal(2)
        out.append(
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=start + timedelta(days=i),
                open=price,
                high=price + Decimal(2),
                low=price - Decimal(2),
                close=price,
                volume=Decimal(100 + i),
            )
        )
    return out


def _patch_repositories(monkeypatch, candles: list[Candle], persisted: list[IndicatorRow]):
    monkeypatch.setattr("app.data.indicator_verification.get_candles", lambda session, symbol, timeframe: candles)
    monkeypatch.setattr(
        "app.data.indicator_verification.get_indicators", lambda session, symbol, timeframe: persisted
    )


def test_no_candles_is_declared_missing_not_an_error(monkeypatch):
    _patch_repositories(monkeypatch, [], [])

    result = verify_symbol_timeframe(session=object(), symbol="BTCUSDT", timeframe="1d")

    assert result.candles_used == 0
    assert result.is_clean


def test_matching_recalculation_is_clean(monkeypatch):
    candles = _candles("BTCUSDT", "1d", n=200)
    expected = calculate_indicators("BTCUSDT", "1d", candles)
    _patch_repositories(monkeypatch, candles, expected)

    result = verify_symbol_timeframe(session=object(), symbol="BTCUSDT", timeframe="1d")

    assert result.candles_used == 200
    assert result.expected_count == len(expected)
    assert result.persisted_count == len(expected)
    assert result.is_clean


def test_missing_row_is_detected(monkeypatch):
    candles = _candles("BTCUSDT", "1d", n=200)
    expected = calculate_indicators("BTCUSDT", "1d", candles)
    persisted = expected[1:]  # falta la primera fila, como si save_indicators hubiera perdido una
    _patch_repositories(monkeypatch, candles, persisted)

    result = verify_symbol_timeframe(session=object(), symbol="BTCUSDT", timeframe="1d")

    assert not result.is_clean
    assert len(result.mismatches) == 1
    assert result.mismatches[0].kind == "faltante"


def test_value_mismatch_is_detected(monkeypatch):
    candles = _candles("BTCUSDT", "1d", n=200)
    expected = calculate_indicators("BTCUSDT", "1d", candles)
    corrupted = list(expected)
    row = corrupted[0]
    corrupted[0] = IndicatorRow(
        row.symbol, row.timeframe, row.calculated_at, row.indicator_name, row.value + Decimal("5")
    )
    _patch_repositories(monkeypatch, candles, corrupted)

    result = verify_symbol_timeframe(session=object(), symbol="BTCUSDT", timeframe="1d")

    assert not result.is_clean
    assert len(result.mismatches) == 1
    assert result.mismatches[0].kind == "valor_distinto"


def test_tiny_rounding_difference_within_tolerance_is_not_flagged(monkeypatch):
    """El valor persistido pasa por NUMERIC(30, 12) en Postgres real, que
    puede redondear distinto al último dígito que Python — no debe
    reportarse como discrepancia real."""
    candles = _candles("BTCUSDT", "1d", n=200)
    expected = calculate_indicators("BTCUSDT", "1d", candles)
    persisted = list(expected)
    row = persisted[0]
    persisted[0] = IndicatorRow(
        row.symbol, row.timeframe, row.calculated_at, row.indicator_name, row.value + Decimal("0.0000000001")
    )
    _patch_repositories(monkeypatch, candles, persisted)

    result = verify_symbol_timeframe(session=object(), symbol="BTCUSDT", timeframe="1d")

    assert result.is_clean


def test_unexpected_extra_row_is_detected(monkeypatch):
    candles = _candles("BTCUSDT", "1d", n=200)
    expected = calculate_indicators("BTCUSDT", "1d", candles)
    extra_row = IndicatorRow("BTCUSDT", "1d", candles[0]["open_time"], "INDICADOR_VIEJO_YA_NO_CALCULADO", Decimal("1"))
    _patch_repositories(monkeypatch, candles, expected + [extra_row])

    result = verify_symbol_timeframe(session=object(), symbol="BTCUSDT", timeframe="1d")

    assert not result.is_clean
    assert any(m.kind == "inesperado" for m in result.mismatches)


def test_rsi_out_of_bounds_is_a_bounds_violation(monkeypatch):
    candles = _candles("BTCUSDT", "1d", n=200)
    expected = calculate_indicators("BTCUSDT", "1d", candles)
    persisted = list(expected)
    for i, row in enumerate(persisted):
        if row.indicator_name == "RSI_14":
            persisted[i] = IndicatorRow(row.symbol, row.timeframe, row.calculated_at, row.indicator_name, Decimal(150))
            break
    _patch_repositories(monkeypatch, candles, persisted)

    result = verify_symbol_timeframe(session=object(), symbol="BTCUSDT", timeframe="1d")

    # También aparece como valor_distinto (difiere del recalculado) y además
    # como invariante roto (fuera de [0, 100]) — ambos chequeos son
    # independientes a propósito.
    assert any(v.reason == "RSI fuera de [0, 100]" for v in result.bounds_violations)


def test_verify_all_runs_every_combination(monkeypatch):
    candles_btc = _candles("BTCUSDT", "1d", n=200)
    expected_btc = calculate_indicators("BTCUSDT", "1d", candles_btc)

    def fake_get_candles(session, symbol, timeframe):
        return candles_btc if (symbol, timeframe) == ("BTCUSDT", "1d") else []

    def fake_get_indicators(session, symbol, timeframe):
        return expected_btc if (symbol, timeframe) == ("BTCUSDT", "1d") else []

    monkeypatch.setattr("app.data.indicator_verification.get_candles", fake_get_candles)
    monkeypatch.setattr("app.data.indicator_verification.get_indicators", fake_get_indicators)

    results = verify_all(session=object(), symbols=["BTCUSDT", "ETHUSDT"], timeframes=["1d", "1h"])

    by_key = {(r.symbol, r.timeframe): r for r in results}
    assert len(results) == 4
    assert by_key[("BTCUSDT", "1d")].is_clean
    assert by_key[("BTCUSDT", "1d")].candles_used == 200
    assert by_key[("ETHUSDT", "1d")].candles_used == 0
    assert by_key[("BTCUSDT", "1h")].candles_used == 0
