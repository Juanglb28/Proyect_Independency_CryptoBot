"""Score cuantitativo paralelo (Fase B — shadow scoring).

Fórmula 100% determinista que puntúa un símbolo candidato usando SOLO
indicadores técnicos ya calculados (Paso 4), sin componente de IA ni
multiplicador de calidad de datos.

Propósito: correr en PARALELO al score del Consolidador (IA) para
comparar ambos durante la futura validación walk-forward (Fase C). El
score cuantitativo se guarda en el `decision_snapshot` como información,
NUNCA como decisión — el score del Consolidador sigue siendo el que
determina si se abre una posición.

Decisiones del usuario (2026-10-08):
- Opción C: mismos 3 factores que el pre-filtro (tendencia, volumen,
  distancia max 90d) + RSI como 4.º factor. Pesos iguales (25% cada uno).
- Sin multiplicador de calidad de datos: score crudo de indicadores.

Normalización: ABSOLUTA con clamp + rescale lineal a [0, 100] — misma
filosofía que el pre-filtro (`app/data/prefilter.py`), con la misma
función `_normalize_linear`.

Límites de normalización:
- Tendencia (promedio de %-precio-vs-SMA 90/120/180): [-25, +25]
- Volumen relativo (ratio): [0.4, 1.6]
- Distancia al máximo de 90 días (%, <= 0): [-20, 0]
- RSI 14: [30, 70]

RSI bounds [30, 70]: el RSI oscila entre 0 y 100, pero los extremos se
alcanzan rara vez. El rango [30, 70] captura la zona útil de variación
diaria: por debajo de 30 (sobreventa extrema) satura en 0, por encima de
70 (sobrecompra extrema) satura en 100. Esto es consistente con la
convención clásica de RSI (30/70 como umbrales estándar) y con la
normalización absoluta del pre-filtro ("un RSI de 85 no vale más que uno
de 70 — ambos indican sobrecompra").

Datos faltantes: si falta cualquiera de los 4 factores, devuelve `None`
en vez de inventar un valor (P3 del plan: cero datos inventados).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

# ---------------------------------------------------------------------------
# Versión de la fórmula. Se guarda con cada resultado en el
# `decision_snapshot` para poder interpretar un score histórico aunque los
# límites se recalibren después (reproducibilidad, sección 15 del plan).
# Si cambia cualquier constante de este módulo, sube la versión.
# ---------------------------------------------------------------------------
QUANTITATIVE_FORMULA_VERSION = "1"

# ---------------------------------------------------------------------------
# Límites de normalización absoluta (ver docstring del módulo).
# ---------------------------------------------------------------------------
TENDENCIA_PCT_BOUNDS: tuple[Decimal, Decimal] = (Decimal("-25"), Decimal("25"))
VOLUMEN_RELATIVO_BOUNDS: tuple[Decimal, Decimal] = (Decimal("0.4"), Decimal("1.6"))
DISTANCIA_MAX_90D_PCT_BOUNDS: tuple[Decimal, Decimal] = (Decimal("-20"), Decimal("0"))
RSI_BOUNDS: tuple[Decimal, Decimal] = (Decimal("30"), Decimal("70"))

_NUMERO_DE_FACTORES = Decimal(4)
_CIEN = Decimal(100)


def _normalize_linear(value: Decimal, bounds: tuple[Decimal, Decimal]) -> Decimal:
    """Recorta `value` a `bounds` y lo reescala linealmente a [0, 100].

    Misma función que en `app/data/prefilter.py` — duplicada a propósito
    para que este módulo sea independiente de la capa `data` (vive en
    `domain/rules`, no debe importar de `data`). Si los bounds o la lógica
    de normalización divergieran entre pre-filtro y score cuantitativo en el
    futuro, cada uno evoluciona sin romper al otro.
    """
    low, high = bounds
    if high <= low:
        raise ValueError(f"bounds inválidos: {bounds}")
    clamped = min(max(value, low), high)
    return (clamped - low) / (high - low) * _CIEN


@dataclass(frozen=True)
class QuantitativeScore:
    """Score cuantitativo paralelo de un símbolo candidato.

    Guarda tanto los valores crudos como los sub-scores normalizados,
    para que el resultado sea auditable — cualquiera puede reconstruir
    `score_final` a mano sin volver a consultar la base de datos.
    """

    symbol: str
    calculated_at: datetime

    # Valores crudos (lo que entró a la fórmula)
    tendencia_pct: Decimal
    volumen_relativo: Decimal
    distancia_max_90d_pct: Decimal
    rsi: Decimal

    # Sub-scores normalizados (0-100 cada uno)
    score_tendencia: Decimal
    score_volumen: Decimal
    score_distancia: Decimal
    score_rsi: Decimal

    # Resultado final (promedio simple de los 4 sub-scores)
    score_final: Decimal

    formula_version: str


def calculate_quantitative_score(
    symbol: str,
    calculated_at: datetime,
    *,
    sma_pct_values: list[Decimal | None],
    volumen_relativo: Decimal | None,
    distancia_max_90d_pct: Decimal | None,
    rsi: Decimal | None,
) -> QuantitativeScore | None:
    """Calcula el score cuantitativo paralelo a partir de indicadores YA
    calculados (nunca velas ni precios crudos).

    `sma_pct_values` son los porcentajes precio-vs-SMA ya calculados por el
    llamador (uno por cada periodo de `SMA_PERIODS`, en el mismo orden).

    Devuelve `None` si falta cualquiera de los 4 factores (alguna SMA
    faltante, volumen relativo faltante, distancia a 90d faltante, o RSI
    faltante): un símbolo con datos incompletos no debe recibir un score
    inventado (P3 del plan).
    """
    # --- Validación de datos completos (P3) ---
    if not sma_pct_values or any(v is None for v in sma_pct_values):
        return None
    if volumen_relativo is None or distancia_max_90d_pct is None or rsi is None:
        return None

    # --- Factor 1: Tendencia (promedio de %-precio-vs-SMA) ---
    tendencia_pct = sum(sma_pct_values, Decimal(0)) / len(sma_pct_values)  # type: ignore[arg-type]

    # --- Normalización de cada factor ---
    score_tendencia = _normalize_linear(tendencia_pct, TENDENCIA_PCT_BOUNDS)
    score_volumen = _normalize_linear(volumen_relativo, VOLUMEN_RELATIVO_BOUNDS)
    score_distancia = _normalize_linear(distancia_max_90d_pct, DISTANCIA_MAX_90D_PCT_BOUNDS)
    score_rsi = _normalize_linear(rsi, RSI_BOUNDS)

    # --- Score final: promedio simple (pesos iguales, 25% cada uno) ---
    # Sumar primero y dividir una sola vez en vez de multiplicar cada
    # sub-score por 0.25: evita el error de redondeo intermedio de
    # `Decimal(1) / Decimal(4)` (que sí tiene representación finita, pero
    # la consistencia con el pre-filtro vale más que la micro-optimización).
    score_final = (
        score_tendencia + score_volumen + score_distancia + score_rsi
    ) / _NUMERO_DE_FACTORES

    return QuantitativeScore(
        symbol=symbol,
        calculated_at=calculated_at,
        tendencia_pct=tendencia_pct,
        volumen_relativo=volumen_relativo,
        distancia_max_90d_pct=distancia_max_90d_pct,
        rsi=rsi,
        score_tendencia=score_tendencia,
        score_volumen=score_volumen,
        score_distancia=score_distancia,
        score_rsi=score_rsi,
        score_final=score_final,
        formula_version=QUANTITATIVE_FORMULA_VERSION,
    )
