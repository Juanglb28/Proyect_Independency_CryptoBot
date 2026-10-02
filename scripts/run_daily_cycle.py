"""Corre un ciclo de análisis real y completo (Paso 7):
pre-filtro -> hasta 5 candidatos -> 4 agentes por candidato -> Consolidador
-> propuestas persistidas con su `decision_snapshot`.

A diferencia de `scripts/run_agent_demo_full_cycle.py` (Paso 6, un solo
símbolo, no persiste nada), este script llama a
`app.orchestration.daily_cycle.run_daily_cycle` — el orquestador real que
sí evalúa a todos los candidatos del pre-filtro a la vez (como haría el
sistema en producción) y sí escribe en `ciclos_analisis`/`propuestas`/
`factores`.

ADVERTENCIA: si el pre-filtro encuentra candidatos, hace hasta 3 llamadas
reales a la API de Anthropic POR CANDIDATO (Sonnet 5.5 x2, Haiku 4.5 x1) más
una llamada al Consolidador (Opus 5.5) — hasta 16 llamadas en el peor caso
de 5 candidatos (sección 4.3 del plan: "12-20 llamadas/día en días con
candidatos"). Si el pre-filtro no encuentra candidatos, el ciclo termina
sin gastar nada.

Uso:
    python -m scripts.run_daily_cycle
"""

from __future__ import annotations

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.logging_config import configure_logging
from app.orchestration.daily_cycle import run_daily_cycle
from app.repositories.ai_calls import get_gasto_acumulado_desde
from app.repositories.analysis import get_factores_de_propuesta, get_propuestas_de_ciclo


def _imprimir_control_de_gastos(session, settings, costo_de_esta_corrida) -> None:
    """Costo de esta corrida (ya lo tiene `result.cost_usd`) + gasto
    acumulado desde el saldo declarado + saldo restante ESTIMADO — nunca el
    saldo real exacto, que solo lo sabe la consola de Anthropic (ver
    `Settings.anthropic_saldo_declarado_usd` sobre por qué esto es una
    estimación, no una cifra exacta)."""
    if settings.anthropic_saldo_declarado_usd is None or settings.anthropic_saldo_declarado_en is None:
        print(
            "\n(Para ver gasto acumulado y saldo estimado restante, configura "
            "ANTHROPIC_SALDO_DECLARADO_USD y ANTHROPIC_SALDO_DECLARADO_EN en "
            ".env — ver README, sección 'Control de gastos de la API de "
            "Anthropic'.)"
        )
        return

    gasto_acumulado = get_gasto_acumulado_desde(session, settings.anthropic_saldo_declarado_en)
    saldo_estimado = settings.anthropic_saldo_declarado_usd - gasto_acumulado

    print(
        f"\nGasto acumulado desde {settings.anthropic_saldo_declarado_en.date()} "
        f"(según lo registrado en llamadas_ia): ${gasto_acumulado} USD"
    )
    print(
        f"Saldo estimado restante (declaraste ${settings.anthropic_saldo_declarado_usd} USD "
        f"el {settings.anthropic_saldo_declarado_en.date()}): ${saldo_estimado} USD "
        "-- ESTIMADO, no la cifra exacta de la consola de Anthropic."
    )


def main() -> int:
    configure_logging()
    settings = get_settings()
    engine = get_engine(settings)
    session_factory = get_sessionmaker(engine)

    with session_scope(session_factory) as session:
        result = run_daily_cycle(session, settings)

        print(f"Ciclo: {result.ciclo_id}")
        print(f"Candidatos evaluados: {', '.join(result.candidatos_evaluados) or '(ninguno)'}")
        if result.candidatos_excluidos_por_calidad:
            print(
                "Excluidos por calidad de datos insuficiente: "
                f"{', '.join(result.candidatos_excluidos_por_calidad)}"
            )
        print(f"Costo total del ciclo: ${result.cost_usd} USD")
        _imprimir_control_de_gastos(session, settings, result.cost_usd)

        if not result.propuesta_ids:
            print("\nSin propuestas este ciclo.")
            return 0

        print(f"\n{len(result.propuesta_ids)} propuesta(s) persistida(s):")
        propuestas = get_propuestas_de_ciclo(session, result.ciclo_id)
        for propuesta in propuestas:
            print(
                f"\n- {propuesta.symbol} ({propuesta.categoria}, {propuesta.horizonte}, "
                f"riesgo {propuesta.nivel_riesgo})"
            )
            print(
                f"  puntaje_base={propuesta.puntaje_base} calidad_datos={propuesta.calidad_datos} "
                f"puntaje_ponderado={propuesta.puntaje_ponderado} "
                f"supera_umbral_entrada={propuesta.supera_umbral_entrada}"
            )
            print(f"  tesis: {propuesta.tesis}")
            factores = get_factores_de_propuesta(session, propuesta.id)
            for factor in factores:
                print(f"    {factor.factor}: peso={factor.peso} puntaje={factor.puntaje}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
