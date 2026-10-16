"""Test de integración de `calculate_indicators_for_all` contra Postgres
real: múltiples símbolos/temporalidades, incluyendo una combinación sin
velas guardadas, y reintento sin duplicación.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.data.exchange_client import Candle
from app.data.indicator_calculator import calculate_indicators
from app.data.indicator_pipeline import calculate_indicators_for_all
from app.repositories.candles import save_candles


def _candles(symbol: str, timeframe: str, n: int) -> list[Candle]:
    start = datetime(2024, 9, 1, tzinfo=timezone.utc)
    price = Decimal("50")
    out = []
    for i in range(n):
        price = price + Decimal(i % 4) - Decimal(1)
        out.append(
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=start + timedelta(days=i),
                open=price,
                high=price + Decimal(1),
                low=price - Decimal(1),
                close=price,
                volume=Decimal(50 + i),
            )
        )
    return out


def test_bulk_pipeline_across_symbols_and_skips_missing_combination(db_session):
    save_candles(db_session, _candles("BTCUSDT", "1d", n=150))
    save_candles(db_session, _candles("BTCUSDT", "1h", n=150))
    # ETHUSDT/1d se deja sin velas a propósito: debe declararse como
    # faltante, no romper la corrida de las demás combinaciones.
    db_session.flush()

    results = calculate_indicators_for_all(
        db_session, symbols=["BTCUSDT", "ETHUSDT"], timeframes=["1d", "1h"]
    )

    by_key = {(r.symbol, r.timeframe): r for r in results}
    assert by_key[("BTCUSDT", "1d")].calculated > 0
    assert by_key[("BTCUSDT", "1h")].calculated > 0
    assert by_key[("ETHUSDT", "1d")].candles_used == 0
    assert by_key[("ETHUSDT", "1h")].candles_used == 0


def test_bulk_pipeline_rerun_does_not_duplicate(db_session):
    save_candles(db_session, _candles("SOLUSDT", "1d", n=150))
    db_session.flush()

    first = calculate_indicators_for_all(db_session, symbols=["SOLUSDT"], timeframes=["1d"])
    second = calculate_indicators_for_all(db_session, symbols=["SOLUSDT"], timeframes=["1d"])

    assert first[0].inserted == first[0].calculated
    assert first[0].inserted > 0
    assert second[0].inserted == 0


def test_incremental_con_cola_inserta_solo_la_vela_nueva_y_coincide_con_completo(db_session):
    """El camino incremental (tail) del ciclo horario: tras calcular toda la
    historia, agregar UNA vela y recalcular con cola debe (a) insertar solo los
    indicadores de la vela nueva y (b) darles el mismo valor que un recálculo
    completo. Las filas viejas no se tocan (ON CONFLICT DO NOTHING)."""
    from decimal import ROUND_HALF_EVEN

    from app.data.indicator_pipeline import (
        VELAS_DE_COLA_INCREMENTAL,
        calculate_indicators_for_symbol_timeframe,
    )
    from app.repositories.indicators import get_indicators

    velas = _candles("BTCUSDT", "1h", n=VELAS_DE_COLA_INCREMENTAL + 60)
    save_candles(db_session, velas[:-1])  # toda la historia menos la última
    db_session.flush()

    # Cálculo completo inicial (historia sin la última vela).
    calculate_indicators_for_symbol_timeframe(db_session, "BTCUSDT", "1h")
    db_session.commit()
    antes = len(get_indicators(db_session, "BTCUSDT", "1h"))

    # Llega la vela nueva; el ciclo horario calcula SOLO con la cola.
    save_candles(db_session, [velas[-1]])
    db_session.flush()
    res = calculate_indicators_for_symbol_timeframe(
        db_session, "BTCUSDT", "1h", tail=VELAS_DE_COLA_INCREMENTAL
    )
    db_session.commit()

    # (a) Cargó solo la cola, no toda la historia.
    assert res.candles_used == VELAS_DE_COLA_INCREMENTAL

    # (b) Se insertaron pocas filas (solo las de la vela nueva), no toda la historia.
    despues = get_indicators(db_session, "BTCUSDT", "1h")
    nuevas = len(despues) - antes
    assert 0 < nuevas <= 6  # RSI, ATR y 3 SMA (y volumen) para una sola vela

    # (c) Los valores de la vela nueva coinciden con un recálculo COMPLETO.
    def _q(v):
        return v.quantize(Decimal("1E-12"), rounding=ROUND_HALF_EVEN)

    completo = {
        (r.indicator_name, r.calculated_at): _q(r.value)
        for r in calculate_indicators("BTCUSDT", "1h", velas)
    }
    ultimo = velas[-1]["open_time"]
    guardado_nuevo = {
        (r.indicator_name, r.calculated_at): _q(r.value)
        for r in despues
        if r.calculated_at == ultimo
    }
    assert guardado_nuevo, "no se guardó ningún indicador de la vela nueva"
    for clave, valor in guardado_nuevo.items():
        assert valor == completo[clave], f"difiere en {clave}: {valor} vs {completo[clave]}"
