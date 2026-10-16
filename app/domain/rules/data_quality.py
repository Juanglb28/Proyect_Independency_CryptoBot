"""Fórmula determinista de calidad de datos (reemplazo de la
`penalizacion_confianza_pct` subjetiva del Agente 4 / Auditor IA).

Motivación: la calidad de los datos con que se evalúa un símbolo era hasta
ahora una opinión del LLM (un número 0-100 que el Auditor IA decidía
libremente), y ese número se usaba como multiplicador del score final en
`scoring.calculate_consolidated_score`. Esto contradice P1 ("el código
calcula") porque un LLM estaba, en la práctica, calculando un factor que
afectaba directamente el puntaje de inversión.

Este módulo reemplaza esa opinión con una fórmula determinista, auditable y
testeable. Los inputs son los MISMOS datos que `candidate_pipeline.
build_datos_auditor` ya prepara para el Auditor IA — no se inventa ninguno.

Concepto "presenta anomalías" (decisión del usuario, 2026-10-08): las
anomalías detectadas se reportan como flag informativo SIN penalización en
el score. El usuario ve "este símbolo presenta anomalías: [lista]" y
decide si investiga o procede sabiendo que existen riesgos ocultos. La
penalización del score viene SOLO de la fórmula de calidad, no de la
presencia de anomalías.

Condiciones bloqueantes: lista cerrada y explícita de situaciones donde los
datos son tan degradados que generar una propuesta no tiene sentido. Estas
SON distintas de las anomalías (que son informativas): un bloqueante detiene
el análisis, una anomalía lo advierte.

Contrato del plan (sección 16): función pura, testeable, independiente de
Streamlit, del LLM y de Binance.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# ---------------------------------------------------------------------------
# Constantes de la fórmula — versión 1
# ---------------------------------------------------------------------------

DATA_QUALITY_FORMULA_VERSION = "1"
"""Se guarda con cada resultado para reproducibilidad (sección 15 del plan).
Si cambia cualquier constante de este módulo, sube la versión."""

# Penalización por campos faltantes: cada campo faltante resta un porcentaje
# fijo del score de calidad. Con 4 temporalidades × ~7 indicadores por
# temporalidad = ~28 campos posibles, la penalización por campo individual
# es moderada — la suma puede llegar a ser severa si faltan muchos.
PENALIZACION_POR_CAMPO_FALTANTE = Decimal("3")
"""Puntos que se restan (sobre 100) por cada campo faltante reportado por
el analista cuantitativo o de eventos."""

# Penalización por antigüedad de datos: la última vela cerrada debería ser
# de hace pocas horas. Si es vieja, los datos no reflejan el estado actual.
# Cero penalización hasta HORAS_DATOS_FRESCOS; penalización máxima a partir
# de HORAS_DATOS_OBSOLETOS.
HORAS_DATOS_FRESCOS = Decimal("26")
"""Hasta estas horas de antigüedad de la última vela, no hay penalización.
26h porque las velas diarias cierran cada 24h y el ciclo puede correr
con un margen de ~2h después del cierre."""

HORAS_DATOS_OBSOLETOS = Decimal("72")
"""A partir de estas horas, la penalización por antigüedad llega a su
máximo. 72h = 3 días sin una vela cerrada."""

PENALIZACION_MAX_ANTIGUEDAD = Decimal("25")
"""Máximo de puntos que se restan por antigüedad de datos."""

# Penalización por huecos en series de velas.
PENALIZACION_POR_HUECO = Decimal("5")
"""Puntos que se restan por cada hueco reportado en las series de velas."""

# Tope de penalización total: la calidad nunca baja de este valor mínimo
# por penalizaciones combinadas (el bloqueo se maneja aparte, con la lista
# cerrada de condiciones bloqueantes).
CALIDAD_MINIMA = Decimal("10")
"""La calidad de datos nunca baja de 10/100 por penalizaciones combinadas.
Un valor menor (o cero) solo viene de un bloqueo explícito."""

_CIEN = Decimal(100)


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DataQualityResult:
    """Resultado de la evaluación determinista de calidad de datos de un
    símbolo en un ciclo.

    `calidad_datos` (0-100): reemplaza directamente a
    `100 - auditor.penalizacion_confianza_pct` como input de
    `scoring.calculate_consolidated_score`.

    `presenta_anomalias`: flag informativo (True/False). Siempre False si
    `anomalias` está vacía; siempre True si tiene al menos una. NO afecta
    `calidad_datos` — es pura información para el usuario.

    `bloqueantes`: lista cerrada de condiciones que impiden generar una
    propuesta para este símbolo. Si no está vacía, el ciclo no debe llamar
    al Consolidador con este candidato (equivalente al antiguo
    `calidad_global == "insuficiente"`, pero determinista y con una lista
    cerrada de causas).

    Se guardan también los subtotales de penalización para auditar de dónde
    salió el número final.
    """

    calidad_datos: Decimal
    """Score de calidad 0-100, para usar como multiplicador en el score."""

    penalizacion_campos: Decimal
    """Puntos restados por campos faltantes."""
    penalizacion_antiguedad: Decimal
    """Puntos restados por antigüedad de la última vela."""
    penalizacion_huecos: Decimal
    """Puntos restados por huecos en series de velas."""

    anomalias: tuple[str, ...]
    """Descripciones textuales de las anomalías detectadas (informativas)."""
    presenta_anomalias: bool
    """True si se detectó al menos una anomalía."""

    bloqueantes: tuple[str, ...]
    """Condiciones que impiden generar una propuesta. Vacía = puede continuar."""

    formula_version: str
    """Versión de la fórmula usada."""


# ---------------------------------------------------------------------------
# Fórmula
# ---------------------------------------------------------------------------

def calculate_data_quality(
    *,
    campos_faltantes_cuantitativo: list[str],
    campos_faltantes_eventos: list[str],
    huecos_en_series: list[str],
    ultima_vela_hace_horas: float | None,
    velas_diarias_disponibles: int,
    eventos_en_catalogo: int,
) -> DataQualityResult:
    """Calcula la calidad de datos de un símbolo a partir de los MISMOS inputs
    que `candidate_pipeline.build_datos_auditor` ya prepara.

    Función pura: no lee la base de datos ni consulta nada. Solo combina lo
    que recibe.

    Parámetros
    ----------
    campos_faltantes_cuantitativo
        Lista de campos que el analista cuantitativo no pudo completar.
    campos_faltantes_eventos
        Lista de campos que el analista de eventos no pudo completar.
    huecos_en_series
        Lista de huecos detectados en las series de velas (cada uno es una
        descripción textual del hueco).
    ultima_vela_hace_horas
        Horas desde el cierre de la última vela diaria hasta ahora.
        `None` si no hay velas diarias.
    velas_diarias_disponibles
        Número total de velas diarias disponibles para este símbolo.
    eventos_en_catalogo
        Número de eventos del catálogo en alcance para este símbolo.
    """
    anomalias: list[str] = []
    bloqueantes: list[str] = []

    # --- Condiciones bloqueantes (lista cerrada) ---

    if velas_diarias_disponibles == 0:
        bloqueantes.append("sin_velas_diarias: no hay ninguna vela diaria para este símbolo")

    if ultima_vela_hace_horas is None:
        bloqueantes.append("sin_fecha_ultima_vela: no se pudo determinar la antigüedad de los datos")

    # Si las velas diarias de la temporalidad principal faltan COMPLETAMENTE,
    # no tiene sentido seguir.
    if "velas_1d" in campos_faltantes_cuantitativo:
        bloqueantes.append("sin_velas_1d_en_datos: la temporalidad diaria no tiene datos en el bloque")

    # --- Penalización por campos faltantes ---

    total_faltantes = len(campos_faltantes_cuantitativo) + len(campos_faltantes_eventos)
    penalizacion_campos = PENALIZACION_POR_CAMPO_FALTANTE * Decimal(total_faltantes)

    if total_faltantes > 0:
        anomalias.append(
            f"campos_faltantes ({total_faltantes}): "
            + ", ".join(campos_faltantes_cuantitativo + campos_faltantes_eventos)
        )

    # --- Penalización por antigüedad ---

    penalizacion_antiguedad = Decimal(0)
    if ultima_vela_hace_horas is not None:
        horas = Decimal(str(ultima_vela_hace_horas))
        if horas > HORAS_DATOS_FRESCOS:
            # Interpolación lineal entre HORAS_DATOS_FRESCOS y HORAS_DATOS_OBSOLETOS
            rango = HORAS_DATOS_OBSOLETOS - HORAS_DATOS_FRESCOS
            exceso = min(horas - HORAS_DATOS_FRESCOS, rango)
            penalizacion_antiguedad = (exceso / rango) * PENALIZACION_MAX_ANTIGUEDAD

            anomalias.append(
                f"datos_antiguos: última vela cerrada hace {float(horas):.1f}h "
                f"(umbral fresco: {HORAS_DATOS_FRESCOS}h)"
            )

    # --- Penalización por huecos en series ---

    total_huecos = len(huecos_en_series)
    penalizacion_huecos = PENALIZACION_POR_HUECO * Decimal(total_huecos)

    if total_huecos > 0:
        anomalias.append(
            f"huecos_en_series ({total_huecos}): " + "; ".join(huecos_en_series)
        )

    # --- Anomalías adicionales (informativas, sin penalización) ---

    if eventos_en_catalogo == 0:
        anomalias.append(
            "sin_eventos_en_catalogo: no hay eventos del catálogo en alcance para este símbolo"
        )

    # --- Score de calidad ---

    penalizacion_total = penalizacion_campos + penalizacion_antiguedad + penalizacion_huecos
    calidad_datos = max(_CIEN - penalizacion_total, CALIDAD_MINIMA)

    # Si hay bloqueantes, la calidad es 0 (el ciclo se detiene para este
    # símbolo, no tiene sentido asignarle un multiplicador).
    if bloqueantes:
        calidad_datos = Decimal(0)

    return DataQualityResult(
        calidad_datos=calidad_datos,
        penalizacion_campos=penalizacion_campos,
        penalizacion_antiguedad=penalizacion_antiguedad,
        penalizacion_huecos=penalizacion_huecos,
        anomalias=tuple(anomalias),
        presenta_anomalias=len(anomalias) > 0,
        bloqueantes=tuple(bloqueantes),
        formula_version=DATA_QUALITY_FORMULA_VERSION,
    )
