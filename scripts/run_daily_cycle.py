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

from datetime import datetime, timezone
from decimal import Decimal

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.logging_config import configure_logging
from app.domain.rules.cycle_summary import OrigenCiclo
from app.domain.rules.recovery import inicio_del_dia_utc
from app.notifications.dispatcher import dispatch_pending_alerts
from app.notifications.telegram import build_sender
from app.domain.rules.ai_budget import saldo_insuficiente
from app.orchestration.ai_budget import leer_saldo_estimado_ia
from app.orchestration.cycle_summary import registrar_ciclo_fallido, registrar_resumen_ciclo
from app.orchestration.daily_cycle import run_daily_cycle
from app.repositories.ai_calls import get_gasto_acumulado_desde
from app.repositories.analysis import get_factores_de_propuesta, get_propuestas_de_ciclo


def _imprimir_control_de_gastos(session, settings, costo_de_esta_corrida) -> None:
    """Costo de esta corrida (ya lo tiene `result.cost_usd`) + gasto
    acumulado desde el saldo declarado + saldo restante ESTIMADO — nunca el
    saldo real exacto, que solo lo sabe la consola de Anthropic (ver
    `Settings.anthropic_saldo_declarado_usd` sobre por qué esto es una
    estimación, no una cifra exacta)."""
    gasto_hoy = get_gasto_acumulado_desde(session, inicio_del_dia_utc(datetime.now(timezone.utc)))
    print(
        f"\nGasto de IA de hoy (UTC): ${gasto_hoy} USD de un tope diario de ${settings.ai_daily_budget_usd} USD "
        "(el tope solo frena los ciclos AUTOMÁTICOS del scheduler; esta corrida manual no se bloquea)."
    )
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


def explicar_sin_propuestas(result) -> list[str]:
    """Líneas de texto que explican POR QUÉ un ciclo terminó sin propuestas.

    Solo describe lo que el resultado del ciclo ya contiene — no consulta la
    base ni interpreta nada nuevo (P1/P3). Los cuatro casos posibles:
    1. el pre-filtro no dejó ningún candidato;
    2. todos los candidatos quedaron excluidos por calidad de datos;
    3. el Consolidador recomendó `no_invertir` (con su razón textual);
    4. cualquier otro caso (no debería ocurrir: el schema rechaza "invertir"
       sin opciones) — se dice tal cual en vez de inventar una causa."""
    if not result.candidatos_evaluados:
        return ["Motivo: el pre-filtro no dejó ningun candidato (ningun simbolo supero el puntaje minimo del pre-filtro)."]
    if result.consolidador_recomendacion is None:
        return [
            "Motivo: todos los candidatos fueron excluidos por calidad de datos insuficiente; "
            "el Consolidador no se llamo."
        ]
    if result.consolidador_recomendacion == "no_invertir":
        lineas = ["Motivo: el Consolidador recomendo NO invertir hoy."]
        lineas.append(f"  Razon del Consolidador: {result.consolidador_razon_no_invertir}")
        if result.consolidador_confianza_global is not None:
            lineas.append(f"  Confianza global del Consolidador: {result.consolidador_confianza_global}")
        return lineas
    return [
        "Motivo no determinado: el Consolidador recomendo "
        f"'{result.consolidador_recomendacion}' pero no se persistio ninguna propuesta (revisar los logs)."
    ]


def _enviar_avisos_pendientes(session_factory, settings) -> None:
    """Manda a Telegram los avisos pendientes (el resumen de esta corrida, y
    cualquier otro que estuviera esperando). Nunca falla la corrida por esto."""
    sender = build_sender(settings.telegram_bot_token, settings.telegram_chat_id)
    if sender is None:
        print("\nTelegram no está configurado: el resumen quedó guardado y se enviará cuando lo esté.")
        return
    try:
        despacho = dispatch_pending_alerts(session_factory, sender, now=datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - un fallo de Telegram no puede tumbar el ciclo
        print(f"\nNo se pudo enviar el aviso a Telegram ({type(exc).__name__}); queda pendiente.")
        return
    if despacho.fallidas:
        print(f"\nTelegram: {despacho.enviadas} enviado(s), {despacho.fallidas} fallido(s); los fallidos se reintentan.")
    elif despacho.enviadas:
        print(f"\nTelegram: {despacho.enviadas} aviso(s) enviado(s) (incluye el resumen de esta corrida).")


def _advertir_saldo_bajo(session_factory, settings) -> None:
    """Avisa ANTES de gastar si el saldo estimado de Anthropic no alcanza para un
    ciclo. Solo advierte: esta corrida la lanza el usuario a propósito (o la tarea
    de Windows, sin nadie delante) y el saldo es una estimación; nunca la bloquea."""
    try:
        with session_scope(session_factory) as session:
            saldo = leer_saldo_estimado_ia(
                session,
                saldo_declarado_usd=settings.anthropic_saldo_declarado_usd,
                saldo_declarado_en=settings.anthropic_saldo_declarado_en,
            )
    except Exception:  # noqa: BLE001 - es solo una advertencia
        return
    if saldo is not None and saldo_insuficiente(saldo, settings.ai_min_balance_per_cycle_usd):
        print(
            f"\nADVERTENCIA: el saldo estimado de Anthropic es ${saldo.quantize(Decimal('0.01'))} USD, menos de los "
            f"${settings.ai_min_balance_per_cycle_usd} USD que suele costar un ciclo. Si el saldo se agota a mitad de "
            "camino el ciclo falla y se cobra lo ya gastado. (Estimación: revisa la consola de Anthropic.)\n"
        )


def main() -> int:
    configure_logging()
    settings = get_settings()
    engine = get_engine(settings)
    session_factory = get_sessionmaker(engine)

    _advertir_saldo_bajo(session_factory, settings)
    try:
        with session_scope(session_factory) as session:
            result = run_daily_cycle(session, settings)
    except Exception as exc:
        registrar_ciclo_fallido(
            session_factory, origen=OrigenCiclo.MANUAL, error=exc, now=datetime.now(timezone.utc)
        )
        _enviar_avisos_pendientes(session_factory, settings)
        raise

    # El resumen se guarda y se envía ANTES de imprimir el detalle: así llega
    # aunque la impresión de abajo falle por cualquier motivo.
    registrar_resumen_ciclo(
        session_factory,
        result,
        origen=OrigenCiclo.MANUAL,
        tope_usd=settings.ai_daily_budget_usd,
        now=datetime.now(timezone.utc),
        saldo_declarado_usd=settings.anthropic_saldo_declarado_usd,
        saldo_declarado_en=settings.anthropic_saldo_declarado_en,
        saldo_minimo_por_ciclo_usd=settings.ai_min_balance_per_cycle_usd,
    )
    _enviar_avisos_pendientes(session_factory, settings)

    with session_scope(session_factory) as session:
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
            for linea in explicar_sin_propuestas(result):
                print(linea)
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
            if propuesta.tier_entrada is not None:
                print(f"  tier_entrada={propuesta.tier_entrada.value}")
            if propuesta.ciclos_consecutivos_en_zona:
                print(
                    f"  ciclos_consecutivos_en_zona={propuesta.ciclos_consecutivos_en_zona} "
                    "(observacion; no afecta la entrada)"
                )
            if propuesta.tier_entrada is not None and propuesta.tier_entrada.value == "MAYOR_RIESGO":
                print("  ** ENTRADA DE MAYOR RIESGO: no supera el umbral normal; requiere aprobacion manual **")
            print(f"  tesis: {propuesta.tesis}")
            factores = get_factores_de_propuesta(session, propuesta.id)
            for factor in factores:
                print(f"    {factor.factor}: peso={factor.peso} puntaje={factor.puntaje}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
