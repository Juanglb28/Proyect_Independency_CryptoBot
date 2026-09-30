"""Asignación de modelo por agente y tabla de precios (sección 7.1 del plan:
"Modelo | Sonnet/Haiku/Opus" por rol — el plan fija el ROL costo/calidad,
no la versión exacta del modelo, que es la que se decide aquí).

Decisión del usuario (2026-09-29), verificada contra el catálogo vigente de
la API de Anthropic en esa fecha (platform.claude.com/docs/en/models/overview):

- Analista cuantitativo (Agente 2) y Analista de eventos (Agente 3):
  Sonnet 5.5 — mismo rol "Sonnet" del plan para ambos.
- Auditor de datos (Agente 4): Haiku 4.5 — el más barato, coherente con ser
  una verificación mecánica de completitud, no de interpretación de mercado.
- Consolidador (Agente 5): Opus 5.5 — el que pondera y decide, el rol más
  exigente de los cinco.

Precios verificados en la misma fuente el 2026-09-29 (USD por millón de
tokens, entrada/salida). Si Anthropic cambia precios, estas constantes
quedan desactualizadas: `cost_usd` en `llamadas_ia` es una cifra de
observabilidad interna, no la factura real (ver docstring de
`app/db/models/ai_calls.py`) — revisar esta tabla si el costo mensual
observado se aleja mucho del estimado del plan (sección 11: "~8 USD/mes").
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

MODEL_ANALISTA = "claude-sonnet-5-5"
MODEL_AUDITOR = "claude-haiku-4-5-20251001"
MODEL_CONSOLIDADOR = "claude-opus-5-5"


@dataclass(frozen=True)
class PrecioPorMillonUSD:
    entrada: Decimal
    salida: Decimal


PRECIOS_USD_POR_MTOK: dict[str, PrecioPorMillonUSD] = {
    MODEL_ANALISTA: PrecioPorMillonUSD(Decimal("2"), Decimal("10")),
    MODEL_AUDITOR: PrecioPorMillonUSD(Decimal("1"), Decimal("5")),
    MODEL_CONSOLIDADOR: PrecioPorMillonUSD(Decimal("4"), Decimal("20")),
}

_UN_MILLON = Decimal(1_000_000)


def calculate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """Costo estimado de una llamada, a partir de la tabla de precios de
    arriba. Lanza `KeyError` si `model` no está en la tabla — preferible a
    devolver 0 o un costo inventado (sección P3 del plan)."""
    precio = PRECIOS_USD_POR_MTOK[model]
    return (Decimal(input_tokens) * precio.entrada + Decimal(output_tokens) * precio.salida) / _UN_MILLON
