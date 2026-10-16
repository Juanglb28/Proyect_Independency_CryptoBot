"""El cálculo incremental (sobre una cola) de indicadores da valores IDÉNTICOS
a los de historia completa para las velas que se solapan — a la escala con que
se guardan (NUMERIC(30,12)).

Es la garantía de corrección del cálculo horario incremental (2026-10-12): los
indicadores de ventana fija (SMA, volumen, distancia 90 d) son exactos por
construcción, y los recursivos de Wilder (RSI, ATR) convergen al mismo valor
cuando la cola trae suficiente calentamiento (``VELAS_DE_COLA_INCREMENTAL``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from app.data.exchange_client import Candle
from app.data.indicator_calculator import (
    INDICATOR_NAME_ATR,
    INDICATOR_NAME_RSI,
    calculate_indicators,
)
from app.data.indicator_pipeline import VELAS_DE_COLA_INCREMENTAL

_ESCALA = Decimal("1E-12")  # NUMERIC(30, 12)


def _como_guardado(valor: Decimal) -> Decimal:
    return valor.quantize(_ESCALA, rounding=ROUND_HALF_EVEN)


def _serie(n: int) -> list[Candle]:
    """Camino de precios determinista y NO periódico (para que RSI/ATR no caigan
    en un caso degenerado)."""
    start = datetime(2022, 1, 1, tzinfo=timezone.utc)
    candles: list[Candle] = []
    precio = Decimal(100)
    for i in range(n):
        # Paso pseudo-aleatorio determinista: dígitos de un producto grande.
        paso = Decimal((i * 7919 + 13) % 23) / Decimal(10) - Decimal("1.1")
        precio = precio + paso
        if precio < Decimal(1):
            precio = Decimal(1)
        candles.append(
            Candle(
                symbol="BTCUSDT",
                timeframe="1h",
                open_time=start + timedelta(hours=i),
                open=precio,
                high=precio + Decimal("0.7"),
                low=precio - Decimal("0.5"),
                close=precio,
                volume=Decimal(100 + (i * 37 % 500)),
            )
        )
    return candles


def _por_clave(rows) -> dict[tuple[str, datetime], Decimal]:
    return {(r.indicator_name, r.calculated_at): _como_guardado(r.value) for r in rows}


# Máximo de velas que el ciclo horario puede añadir de una vez: su ventana de
# backfill es de 3 días; en 1h son 72. Las velas "nuevas" caen SIEMPRE en la
# zona caliente de la cola (calentamiento >= 1000 - 72 = 928). La zona fría del
# principio corresponde a velas ya guardadas, que ON CONFLICT DO NOTHING protege.
_MAX_VELAS_NUEVAS_POR_CORRIDA = 72


def test_las_velas_nuevas_salen_identicas_a_historia_completa():
    serie = _serie(1500)
    completo = _por_clave(calculate_indicators("BTCUSDT", "1h", serie))
    cola = _por_clave(calculate_indicators("BTCUSDT", "1h", serie[-VELAS_DE_COLA_INCREMENTAL:]))

    # Las últimas velas (con margen holgado sobre las que el ciclo horario puede
    # añadir) coinciden EXACTO a 12 decimales con la historia completa.
    nuevas = {c["open_time"] for c in serie[-(_MAX_VELAS_NUEVAS_POR_CORRIDA + 150):]}
    comparadas = 0
    for (nombre, cuando), valor in cola.items():
        if cuando in nuevas:
            assert (nombre, cuando) in completo, f"{nombre}@{cuando} no está en el completo"
            assert valor == completo[(nombre, cuando)], (
                f"difieren en {nombre}@{cuando}: cola={valor} completo={completo[(nombre, cuando)]}"
            )
            comparadas += 1
    assert comparadas > 0, "no se comparó ninguna vela nueva"


def test_la_vela_mas_nueva_tiene_rsi_y_atr_y_coinciden():
    serie = _serie(1500)
    ultimo = serie[-1]["open_time"]
    completo = _por_clave(calculate_indicators("BTCUSDT", "1h", serie))
    cola = _por_clave(calculate_indicators("BTCUSDT", "1h", serie[-VELAS_DE_COLA_INCREMENTAL:]))

    for nombre in (INDICATOR_NAME_RSI, INDICATOR_NAME_ATR):
        assert (nombre, ultimo) in completo and (nombre, ultimo) in cola
        assert cola[(nombre, ultimo)] == completo[(nombre, ultimo)]


def test_una_cola_corta_SI_cambia_el_rsi_recursivo():
    # Guarda de que el test tiene filo: con poco calentamiento el RSI/ATR de
    # Wilder NO coincide (por eso la cola es grande, no de unas pocas velas).
    serie = _serie(1500)
    ultimo = serie[-1]["open_time"]
    completo = _por_clave(calculate_indicators("BTCUSDT", "1h", serie))
    cola_corta = _por_clave(calculate_indicators("BTCUSDT", "1h", serie[-30:]))

    assert cola_corta[(INDICATOR_NAME_RSI, ultimo)] != completo[(INDICATOR_NAME_RSI, ultimo)]


def test_el_calentamiento_de_1000_supera_lo_que_pide_la_convergencia():
    # (13/14)^382 < 0.5e-12; 1000 deja margen de sobra. Documentado como número.
    assert VELAS_DE_COLA_INCREMENTAL >= 382
