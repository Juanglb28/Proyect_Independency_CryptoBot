"""Corre el Agente 4 (Auditor de datos) UNA vez contra la API real de
Anthropic, con un bloque DATOS de ejemplo escrito a mano (sección 7 del
plan, Paso 6).

Por qué un DATOS de ejemplo y no datos reales de la base: armar el bloque
DATOS a partir de `velas`/`indicadores`/`analisis_eventos` de un símbolo
real es responsabilidad de la orquestación (Paso 7,
`app/orchestration/candidate_pipeline.py`, todavía no implementado) — este
script solo demuestra que el tubo completo funciona (cliente Anthropic ->
parseo -> validación Pydantic -> `llamadas_ia`), no reemplaza esa
orquestación.

Se eligió el Auditor porque es el más barato de los tres agentes de este
bloque (Haiku 4.5) y no depende de ningún otro agente.

ADVERTENCIA: esto SÍ hace una llamada real a la API de Anthropic y SÍ
consume crédito de pago (no la suscripción Max — ver sección 11 del plan).
Cuesta una fracción de centavo, pero requiere ANTHROPIC_API_KEY configurada
en tu `.env`.

Uso:
    python -m scripts.run_agent_demo
"""

from __future__ import annotations

from app.ai.agents.auditor import run_auditor_datos
from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.logging_config import configure_logging

# Ejemplo ilustrativo, no un caso real extraído de la base de datos (ver
# docstring del módulo). Deliberadamente incompleto en un par de campos,
# para poder ver que el auditor efectivamente los señala en vez de
# ignorarlos.
DATOS_DE_EJEMPLO = {
    "simbolo": "AVAXUSDT",
    "velas_diarias_disponibles": 620,
    "hueco_mas_reciente_en_velas": None,
    "ultima_vela_hace_horas": 1.2,
    "indicadores_calculados": ["RSI_14", "ATR_14", "SMA_90", "SMA_120", "SMA_180", "VOLUMEN_RELATIVO_30"],
    "indicador_faltante": "DISTANCIA_MAX_90D",
    "eventos_comparables_en_catalogo": 2,
    "valor_sospechoso_detectado": None,
}


def main() -> int:
    configure_logging()
    settings = get_settings()
    engine = get_engine(settings)
    session_factory = get_sessionmaker(engine)

    with session_scope(session_factory) as session:
        result = run_auditor_datos(session, settings, DATOS_DE_EJEMPLO)

    print(f"llamada_id: {result.llamada_id}")
    print(f"tokens: entrada={result.input_tokens} salida={result.output_tokens}")
    print(f"costo estimado: ${result.cost_usd} USD")
    print(f"\nsalida validada:\n{result.output.model_dump_json(indent=2)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
