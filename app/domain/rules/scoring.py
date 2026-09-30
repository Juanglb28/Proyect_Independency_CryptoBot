"""Umbrales de puntuación fijos (sección 19 del plan, tabla "REGLAS FIJAS").

Viven en código versionado, no en `.env` (sección 10: "no conviertas reglas
críticas... en simples variables de entorno"). A diferencia de las reglas de
`risk.py` (stop, take profit, drawdown), estas dos no mueven dinero
directamente — controlan el flujo del ciclo de análisis (cuántos símbolos
pasan del pre-filtro determinista a los agentes de IA) — pero igual quedan
aquí y no dispersas en el código que las usa, por la misma razón: son
números que no deben poder cambiarse "al pasar" dentro de una función.

Estos dos valores YA estaban fijados en el plan desde el inicio (sección 19)
y no forman parte de la fórmula del pre-filtro decidida en esta sesión
(sección 6.1/17 dejaba pendiente el CÓMO se calcula el puntaje, no estos
umbrales sobre el resultado ya calculado).

`calculate_consolidated_score` (agregado junto con el Agente 5 —
Consolidador, Paso 6) es la aplicación directa de P1 del plan ("el código
calcula, la IA interpreta") a la ponderación de esa etapa: el Consolidador
(un LLM) NUNCA hace esta suma ni aplica el multiplicador de calidad de
datos — solo entrega su juicio cualitativo de cada categoría como un
número 0-100, y esta función, determinista y testeada, calcula el
resultado. Ver `app/ai/agents/consolidator.py`.

Esto resuelve además una contradicción real dentro de la sección 7.2 del
plan: la tabla "ESQUEMA DE PONDERACIÓN" original lista 5 categorías que
suman 100% (incluida "Calidad y confianza de datos: 15%"), pero el texto
que la acompaña describe la calidad de datos como un MULTIPLICADOR sobre
el puntaje final ("un puntaje alto sustentado en datos débiles debe
terminar bajo... aplica la penalización... sobre el puntaje final, no
sobre categorías individuales") — son dos modelos matemáticos distintos.
Decisión del usuario (2026-09-29): se adopta el modelo multiplicativo,
con 4 categorías (sin la de calidad, que pasa a ser el multiplicador) y
pesos 30/20/20/30."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

SCORE_MINIMO_PREFILTRO = 45
"""Puntaje mínimo (escala 0-100, ver `app/data/prefilter.py`) para que un
símbolo se considere candidato tras el pre-filtro determinista. Por debajo
de esto, el símbolo no llega a los agentes de IA ese ciclo. Si ningún
símbolo lo supera, el ciclo termina sin una sola llamada a la API (sección
4.2 del plan)."""

MAX_CANDIDATOS_PREFILTRO = 5
"""Máximo de candidatos que el pre-filtro entrega a la fase de agentes de
IA por ciclo (sección 19: "Máximo candidatos IA: 5"), incluso si más de 5
símbolos superan `SCORE_MINIMO_PREFILTRO` ese día."""

SCORE_MINIMO_ENTRADA = 55
"""Puntaje ponderado mínimo (sección 19: "Score mínimo entrada: 55") para
que una opción del Consolidador pueda convertirse en una `propuesta` real
(Paso 7) — coincide con el prompt del Agente 5: "Si ningún activo supera
55 puntos, la recomendación correcta es no invertir". La verificación
final de este umbral es responsabilidad del código (`supera_umbral_entrada`
más abajo), nunca del juicio del LLM sobre su propia recomendación (P2 del
plan: validación determinista después de la interpretación de la IA)."""

# Pesos del score_base del Consolidador (sección 7.2 del plan, "ESQUEMA DE
# PONDERACIÓN", con la corrección del modelo multiplicativo — ver el
# docstring del módulo). Decisión del usuario, 2026-09-29.
PESO_TENDENCIA_MOMENTUM = Decimal("0.30")
PESO_VOLUMEN_LIQUIDEZ = Decimal("0.20")
PESO_POSICION_RANGOS_ATH = Decimal("0.20")
PESO_CONTEXTO_EVENTOS = Decimal("0.30")

_CIEN = Decimal(100)
_SUMA_PESOS_CONSOLIDADOR = (
    PESO_TENDENCIA_MOMENTUM + PESO_VOLUMEN_LIQUIDEZ + PESO_POSICION_RANGOS_ATH + PESO_CONTEXTO_EVENTOS
)
assert _SUMA_PESOS_CONSOLIDADOR == Decimal("1.00"), "los pesos del Consolidador deben sumar 100%"


@dataclass(frozen=True)
class ScoreConsolidado:
    """Resultado de aplicar la fórmula fija del Consolidador a las 4
    sub-puntuaciones que entrega el agente (su juicio, no su aritmética) y
    a la calidad de datos (que viene del Auditor, no del Consolidador —
    ver `app/ai/agents/consolidator.py`).

    Se guardan también `puntaje_base` y `calidad_datos`, no solo el
    resultado final: sin ellos no se puede distinguir, ante un
    `puntaje_ponderado` bajo, si el activo era malo, los datos eran
    malos, o ambos (auditabilidad — pedido explícito del usuario)."""

    puntaje_base: Decimal
    calidad_datos: Decimal
    puntaje_ponderado: Decimal
    supera_umbral_entrada: bool


def _validar_0_a_100(nombre: str, valor: Decimal) -> None:
    if not (Decimal(0) <= valor <= _CIEN):
        raise ValueError(f"{nombre} debe estar en [0, 100], recibido {valor}")


def calculate_consolidated_score(
    *,
    tendencia_momentum: Decimal,
    volumen_liquidez: Decimal,
    posicion_rangos_ath: Decimal,
    contexto_eventos: Decimal,
    calidad_datos: Decimal,
) -> ScoreConsolidado:
    """Combina las 4 sub-puntuaciones del Consolidador con los pesos fijos
    de arriba, y aplica `calidad_datos` como multiplicador sobre el
    resultado (nunca como una quinta categoría sumada — ver el docstring
    del módulo sobre la contradicción que esto resuelve).

    Todos los parámetros deben venir en la escala 0-100 — lanza
    `ValueError` si alguno está fuera de rango, en vez de recortarlo en
    silencio: un valor fuera de [0, 100] indica un error de quien llama
    (el agente no debería producir eso nunca, dado su schema Pydantic),
    no un caso normal a tolerar.
    """
    for nombre, valor in (
        ("tendencia_momentum", tendencia_momentum),
        ("volumen_liquidez", volumen_liquidez),
        ("posicion_rangos_ath", posicion_rangos_ath),
        ("contexto_eventos", contexto_eventos),
        ("calidad_datos", calidad_datos),
    ):
        _validar_0_a_100(nombre, Decimal(valor))

    puntaje_base = (
        Decimal(tendencia_momentum) * PESO_TENDENCIA_MOMENTUM
        + Decimal(volumen_liquidez) * PESO_VOLUMEN_LIQUIDEZ
        + Decimal(posicion_rangos_ath) * PESO_POSICION_RANGOS_ATH
        + Decimal(contexto_eventos) * PESO_CONTEXTO_EVENTOS
    )
    factor_calidad = Decimal(calidad_datos) / _CIEN
    puntaje_ponderado = puntaje_base * factor_calidad

    return ScoreConsolidado(
        puntaje_base=puntaje_base,
        calidad_datos=Decimal(calidad_datos),
        puntaje_ponderado=puntaje_ponderado,
        supera_umbral_entrada=puntaje_ponderado >= SCORE_MINIMO_ENTRADA,
    )
