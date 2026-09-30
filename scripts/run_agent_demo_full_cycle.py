"""Corre los 4 agentes no conversacionales EN SECUENCIA (cuantitativo ->
eventos -> auditor -> consolidador) contra datos REALES de un símbolo, para
verificar que el tubo completo funciona con datos reales.

A diferencia de `scripts/run_agent_demo.py` (un único agente, con un bloque
DATOS de ejemplo escrito a mano), este script:

1. Elige un símbolo real — por defecto, el mejor candidato del pre-filtro
   de hoy (`app/data/prefilter_pipeline.py`); o el que se pase con
   `--simbolo`.
2. Arma el bloque DATOS de cada agente leyendo `velas`/`indicadores`/
   `eventos`/`analisis_eventos` YA calculados en la base de datos real —
   nunca inventa un valor: un indicador o una vela ausente se declara en
   una lista de "faltantes", nunca se rellena (sección P3 del plan). Esta
   lógica vive en `app/orchestration/candidate_pipeline.py` (Paso 7) — este
   script solo la invoca, no la duplica.
3. Llama a los 4 agentes de verdad, en el orden real de dependencia
   (2 y 3 son independientes entre sí; 4 audita lo que 2 y 3 recibieron;
   5 consolida las tres salidas). El puntaje final del Consolidador se
   calcula en código (`app/domain/rules/scoring.py`), nunca por el LLM.

Sigue sin persistir nada en `factores`/`propuestas`/`ciclos_analisis` —
sigue siendo un demo de un solo símbolo, no el ciclo real (para eso está
`app/orchestration/daily_cycle.py`, que sí persiste y sí evalúa varios
candidatos a la vez). Solo `llamadas_ia` queda registrada (mismo registro
de auditoría que ya usa cualquier llamada a un agente).

**Limitación deliberada de este demo**: solo evalúa UN símbolo a la vez, así
que el Consolidador solo puede proponer opciones para ese símbolo (nunca
elegiría entre varios candidatos, como haría un ciclo real con los 4-5 que
entrega el pre-filtro). Si el modelo intentara proponer un símbolo distinto
al analizado, `run_consolidador` lanza `KeyError` en vez de asumir una
calidad de datos por defecto — ver su docstring.

ADVERTENCIA: hace 4 llamadas reales a la API de Anthropic (Sonnet 5.5 x2,
Haiku 4.5 x1, Opus 5.5 x1) y consume crédito de pago (no la suscripción Max
— ver sección 11 del plan). El costo total se imprime al final; con el
Consolidador en Opus siendo el más caro, debería quedar por debajo de unos
pocos centavos de dólar para un símbolo con datos completos.

Uso:
    python -m scripts.run_agent_demo_full_cycle
    python -m scripts.run_agent_demo_full_cycle --simbolo ETHUSDT
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from app.ai.agents.auditor import run_auditor_datos
from app.ai.agents.consolidator import run_consolidador
from app.ai.agents.events import run_analista_eventos
from app.ai.agents.quantitative import run_analista_cuantitativo
from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.logging_config import configure_logging
from app.orchestration.candidate_pipeline import (
    build_datos_analista_cuantitativo,
    build_datos_analista_eventos,
    build_datos_auditor,
    pick_symbol,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--simbolo",
        help="Símbolo a analizar (por defecto: el mejor candidato del pre-filtro de hoy).",
    )
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()
    engine = get_engine(settings)
    session_factory = get_sessionmaker(engine)

    with session_scope(session_factory) as session:
        symbol = pick_symbol(session, args.simbolo)
        print(f"Símbolo analizado: {symbol}\n")

        datos_cuant, faltantes_cuant = build_datos_analista_cuantitativo(session, symbol)
        datos_evt, faltantes_evt = build_datos_analista_eventos(session, symbol)
        datos_aud = build_datos_auditor(
            session,
            symbol,
            faltantes_cuant,
            faltantes_evt,
            len(datos_evt["eventos_del_catalogo_en_alcance"]),
        )

        costo_total = Decimal(0)

        print("=== Agente 2: Analista cuantitativo (Sonnet 5.5) ===")
        r2 = run_analista_cuantitativo(session, settings, datos_cuant)
        costo_total += r2.cost_usd
        print(r2.output.model_dump_json(indent=2))

        print("\n=== Agente 3: Analista de eventos (Sonnet 5.5) ===")
        r3 = run_analista_eventos(session, settings, datos_evt)
        costo_total += r3.cost_usd
        print(r3.output.model_dump_json(indent=2))

        print("\n=== Agente 4: Auditor de datos (Haiku 4.5) ===")
        r4 = run_auditor_datos(session, settings, datos_aud)
        costo_total += r4.cost_usd
        print(r4.output.model_dump_json(indent=2))

        if r4.output.calidad_global == "insuficiente":
            print(
                "\nEl Auditor marcó calidad_global='insuficiente'. Según su propio prompt "
                "(sección 7.2 del plan), un ciclo real (app/orchestration/daily_cycle.py) se "
                "detendría aquí sin generar ninguna propuesta. Este demo sigue de todos modos "
                "hasta el Consolidador, para poder inspeccionar su comportamiento."
            )

        calidad_datos = Decimal(100) - Decimal(str(r4.output.penalizacion_confianza_pct))

        datos_consolidador = {
            "simbolo": symbol,
            "analisis_cuantitativo": r2.output.model_dump(),
            "analisis_eventos": r3.output.model_dump(),
            "auditoria_datos": r4.output.model_dump(),
        }

        print("\n=== Agente 5: Consolidador (Opus 5.5) ===")
        r5 = run_consolidador(
            session,
            settings,
            datos_consolidador,
            calidad_datos_por_simbolo={symbol: calidad_datos},
        )
        costo_total += r5.agent_result.cost_usd
        print(r5.output.model_dump_json(indent=2))

        print("\n--- Puntaje calculado en código (nunca por el LLM) ---")
        if not r5.opciones_evaluadas:
            print("(sin opciones — el Consolidador recomendó no invertir)")
        for evaluada in r5.opciones_evaluadas:
            print(
                f"{evaluada.opcion.simbolo}: "
                f"puntaje_base={evaluada.score.puntaje_base} "
                f"calidad_datos={evaluada.score.calidad_datos} "
                f"puntaje_ponderado={evaluada.score.puntaje_ponderado} "
                f"supera_umbral_entrada={evaluada.score.supera_umbral_entrada}"
            )

        print(f"\nCosto total estimado del ciclo (4 llamadas): ${costo_total} USD")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
