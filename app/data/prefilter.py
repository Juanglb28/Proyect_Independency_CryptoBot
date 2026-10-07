"""Pre-filtro determinista (sección 4.2 / 6.1 / 17 del plan, Paso 6.1).

Contrato del plan (sección 16, "CONTRATOS DETERMINISTAS"): función pura,
testeable, independiente de Streamlit, del LLM y de Binance. Recibe
valores de indicadores YA calculados (Paso 4) y devuelve un puntaje 0-100
por símbolo. No lee la base de datos ni decide qué temporalidad usar —
eso es responsabilidad de `app/data/prefilter_pipeline.py`.

Fórmula (decisiones de esta sesión, 2026-09-29, confirmadas una por una con
el usuario tras revisar la distribución real de indicadores de los 20
símbolos con `scripts/inspect_prefilter_inputs.py` — el plan dejaba
explícitamente pendiente "la fórmula del pre-filtro... hasta ver estos
valores corriendo con datos reales", secciones 6.1/17):

- Alcance: SOLO velas diarias (`1d`). El plan calcula tendencia/volumen en
  las 4 temporalidades, pero los datos reales mostraron escalas
  incompatibles entre ellas (1h con variación típica de ~8%; 12h/1d
  rondando 130-180% de máximo) sin ningún criterio de ponderación entre
  temporalidades definido en el plan — usarlas todas habría significado
  inventar ese criterio. Diario es la temporalidad de mayor contexto y la
  única en la que además se calculan las distancias a rangos.

- Normalización: ABSOLUTA, con límites fijos por métrica (no percentil
  relativo entre los símbolos del día). Esto es lo que permite que un día
  de mercado débil, con los 20 símbolos moviéndose poco, produzca CERO
  candidatos — un percentil relativo, en cambio, siempre reparte una
  distribución 0-100 completa entre los 20 símbolos sin importar qué tan
  débil sea el mercado, lo cual contradice la sección 4.2 del plan ("el
  ciclo puede terminar sin ningún candidato").

- Pesos: iguales entre tendencia, volumen relativo y distancia a rangos
  (promedio simple de los tres sub-puntajes, no una suma ponderada por
  1/3 precomputado — ver el comentario en `calculate_prefilter_score`
  sobre por qué, sección 15.3 del plan: precisión `Decimal`). Punto de
  partida neutral, ajustable más adelante con evidencia de backtesting —
  no hay todavía ninguna razón basada en datos para preferir un factor
  sobre otro.

- `DISTANCIA_ATH` queda EXCLUIDA de esta primera versión: está limitada por
  la ventana de ingesta de ~2 años (no es el ATH real — ver el docstring de
  `indicator_calculator.py`), y con los datos reales de hoy sale con
  mediana -61.5%, prácticamente sin poder discriminar entre símbolos. Se
  usa únicamente `DISTANCIA_MAX_90D`, que con los datos reales de hoy sí
  varía de forma útil (mediana -6.3%, rango -17.5% a -0.67%).

Límites de normalización (primer corte, grounded en los percentiles reales
observados; TODO — ajustable si el universo o el régimen de mercado
cambian mucho; volver a correr `scripts/inspect_prefilter_inputs.py` para
recalibrar):

- tendencia (promedio de %-precio-vs-SMA en 90/120/180): [-25, +25]
- volumen relativo (ratio, ya sin transformar): [0.4, 1.6]
- distancia al máximo de 90 días (%, típicamente <= 0): [-20, 0]

Todo valor fuera de estos límites se recorta (clamp) al límite más cercano
antes de reescalar a 0-100 — un símbolo con tendencia +40% no vale "más"
que uno con tendencia +25%, ambos saturan en 100.

Símbolos con algún dato faltante (falta alguna SMA, falta el volumen
relativo, o falta `DISTANCIA_MAX_90D`) NO reciben un puntaje: se omiten en
vez de asignarles 50 (neutro) o cualquier otro valor de relleno (sección
P3 del plan, cero datos inventados).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.enums import EstadoPrefiltro
from app.domain.rules.scoring import MAX_CANDIDATOS_PREFILTRO, SCORE_MINIMO_PREFILTRO

# Versión de la fórmula del pre-filtro (límites de normalización, pesos iguales,
# solo velas diarias, umbral y tope). Se guarda con cada resultado en
# `prefiltro_resultados` para poder interpretar un puntaje histórico aunque los
# límites se recalibren después (reproducibilidad, sección 15 del plan). Si
# cambia cualquier constante de este módulo o de `scoring`, sube la versión;
# `tests/unit/test_prefilter.py` lo vigila con una huella.
PREFILTER_FORMULA_VERSION = "1"

# Límites de normalización absoluta (ver docstring del módulo).
TENDENCIA_PCT_BOUNDS: tuple[Decimal, Decimal] = (Decimal("-25"), Decimal("25"))
VOLUMEN_RELATIVO_BOUNDS: tuple[Decimal, Decimal] = (Decimal("0.4"), Decimal("1.6"))
DISTANCIA_MAX_90D_PCT_BOUNDS: tuple[Decimal, Decimal] = (Decimal("-20"), Decimal("0"))

_NUMERO_DE_FACTORES = Decimal(3)
_CIEN = Decimal(100)


def _normalize_linear(value: Decimal, bounds: tuple[Decimal, Decimal]) -> Decimal:
    """Recorta `value` a `bounds` y lo reescala linealmente a [0, 100]."""
    low, high = bounds
    if high <= low:
        raise ValueError(f"bounds inválidos: {bounds}")
    clamped = min(max(value, low), high)
    return (clamped - low) / (high - low) * _CIEN


@dataclass(frozen=True)
class PrefilterScore:
    """Puntaje de pre-filtro de un símbolo en una fecha (sección 4.2 del
    plan).

    Guarda tanto los valores crudos como los sub-puntajes normalizados,
    para que el resultado sea auditable: cualquiera puede reconstruir
    `score_final` a mano a partir de `tendencia_pct` / `volumen_relativo` /
    `distancia_max_90d_pct` sin volver a consultar la base de datos.
    """

    symbol: str
    calculated_at: datetime

    tendencia_pct: Decimal
    volumen_relativo: Decimal
    distancia_max_90d_pct: Decimal

    score_tendencia: Decimal
    score_volumen: Decimal
    score_distancia: Decimal
    score_final: Decimal


def calculate_prefilter_score(
    symbol: str,
    calculated_at: datetime,
    *,
    sma_pct_values: list[Decimal | None],
    volumen_relativo: Decimal | None,
    distancia_max_90d_pct: Decimal | None,
) -> PrefilterScore | None:
    """Calcula el puntaje de pre-filtro de un símbolo a partir de
    indicadores YA calculados (nunca velas ni precios crudos).

    `sma_pct_values` son los porcentajes precio-vs-SMA ya calculados por el
    llamador (uno por cada periodo de `SMA_PERIODS`, en el mismo orden) —
    este módulo no sabe leer indicadores ni consultar la base de datos,
    solo combina lo que recibe.

    Devuelve `None` si falta cualquiera de los tres factores (alguna SMA
    faltante, volumen relativo faltante, o distancia a 90d faltante): un
    símbolo con datos incompletos no debe recibir un puntaje inventado
    (sección P3 del plan).
    """
    if not sma_pct_values or any(v is None for v in sma_pct_values):
        return None
    if volumen_relativo is None or distancia_max_90d_pct is None:
        return None

    tendencia_pct = sum(sma_pct_values, Decimal(0)) / len(sma_pct_values)  # type: ignore[arg-type]

    score_tendencia = _normalize_linear(tendencia_pct, TENDENCIA_PCT_BOUNDS)
    score_volumen = _normalize_linear(volumen_relativo, VOLUMEN_RELATIVO_BOUNDS)
    score_distancia = _normalize_linear(distancia_max_90d_pct, DISTANCIA_MAX_90D_PCT_BOUNDS)

    # Promedio simple en vez de multiplicar cada sub-puntaje por un peso
    # 1/3 precomputado: `Decimal(1) / Decimal(3)` se redondea a un decimal
    # finito (0.3333...3), y multiplicar tres veces por ese valor redondeado
    # acumula un error (p. ej. 50 exacto termina en 49.999999999999999999999998).
    # Sumar primero y dividir una sola vez evita ese arrastre — para pesos
    # iguales es exactamente la misma fórmula, sin el error de redondeo
    # intermedio.
    score_final = (score_tendencia + score_volumen + score_distancia) / _NUMERO_DE_FACTORES

    return PrefilterScore(
        symbol=symbol,
        calculated_at=calculated_at,
        tendencia_pct=tendencia_pct,
        volumen_relativo=volumen_relativo,
        distancia_max_90d_pct=distancia_max_90d_pct,
        score_tendencia=score_tendencia,
        score_volumen=score_volumen,
        score_distancia=score_distancia,
        score_final=score_final,
    )


def select_candidates(scores: list[PrefilterScore]) -> list[PrefilterScore]:
    """Filtra por `SCORE_MINIMO_PREFILTRO` y se queda con los mejores
    `MAX_CANDIDATOS_PREFILTRO` (sección 19 del plan).

    Puede devolver una lista vacía: un día de mercado débil, sin ningún
    símbolo por encima del umbral, es un resultado válido y esperado
    (sección 4.2 del plan) — el ciclo termina ahí, sin gastar ninguna
    llamada a la IA.

    Empate en `score_final`: se ordena también por `symbol` ascendente,
    para que el resultado sea 100% determinista y reproducible entre
    corridas (sección 15 del plan) en vez de depender del orden de
    llegada o de un sort inestable.
    """
    candidates = [s for s in scores if s.score_final >= SCORE_MINIMO_PREFILTRO]
    candidates.sort(key=lambda s: (-s.score_final, s.symbol))
    return candidates[:MAX_CANDIDATOS_PREFILTRO]


@dataclass(frozen=True)
class ResultadoPrefiltro:
    """Lo que se guarda de UN símbolo en un ciclo: su estado, su posición en el
    ranking y, si tuvo puntaje, los valores crudos y los sub-puntajes (los
    mismos de `PrefilterScore`). Sin puntaje (`SIN_DATOS`) esos campos son
    `None`, nunca un valor de relleno."""

    symbol: str
    estado: EstadoPrefiltro
    ranking: int | None
    """1 = mejor puntaje del día entre los símbolos con puntaje (empates por
    símbolo ascendente, igual que `select_candidates`). `None` si no hubo puntaje."""
    score: PrefilterScore | None


def clasificar_resultados(
    scores: list[PrefilterScore], candidates: list[PrefilterScore], symbols_sin_datos: list[str]
) -> list[ResultadoPrefiltro]:
    """Une el resultado del pre-filtro en una lista por símbolo, lista para
    persistir. Función pura: no decide nada nuevo; solo etiqueta lo que
    `calculate_prefilter_score` y `select_candidates` ya decidieron.

    Orden de salida: primero los símbolos con puntaje (por ranking) y luego los
    `SIN_DATOS` (por símbolo), para que el resultado sea reproducible.
    """
    elegidos = {c.symbol for c in candidates}
    ordenados = sorted(scores, key=lambda s: (-s.score_final, s.symbol))

    resultados: list[ResultadoPrefiltro] = []
    for posicion, score in enumerate(ordenados, start=1):
        if score.symbol in elegidos:
            estado = EstadoPrefiltro.CANDIDATO
        elif score.score_final < SCORE_MINIMO_PREFILTRO:
            estado = EstadoPrefiltro.DESCARTADO_POR_PUNTAJE
        else:
            estado = EstadoPrefiltro.DESCARTADO_POR_TOPE
        resultados.append(ResultadoPrefiltro(score.symbol, estado, posicion, score))
    for symbol in sorted(symbols_sin_datos):
        resultados.append(ResultadoPrefiltro(symbol, EstadoPrefiltro.SIN_DATOS, None, None))
    return resultados
