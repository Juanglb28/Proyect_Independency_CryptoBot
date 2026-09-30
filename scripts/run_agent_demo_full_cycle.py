"""Corre los 4 agentes no conversacionales EN SECUENCIA (cuantitativo ->
eventos -> auditor -> consolidador) contra datos REALES de un símbolo, para
verificar que el tubo completo funciona antes de comprometerse al esquema
de persistencia del Paso 7 (orquestación permanente: `daily_cycle.py`,
`candidate_pipeline.py`, tablas `ciclos_analisis`/`factores`/`propuestas`).

A diferencia de `scripts/run_agent_demo.py` (un único agente, con un bloque
DATOS de ejemplo escrito a mano), este script:

1. Elige un símbolo real — por defecto, el mejor candidato del pre-filtro
   de hoy (`app/data/prefilter_pipeline.py`); o el que se pase con
   `--simbolo`.
2. Arma el bloque DATOS de cada agente leyendo `velas`/`indicadores`/
   `eventos`/`analisis_eventos` YA calculados en la base de datos real —
   nunca inventa un valor: un indicador o una vela ausente se declara en
   una lista de "faltantes", nunca se rellena (sección P3 del plan).
3. Llama a los 4 agentes de verdad, en el orden real de dependencia
   (2 y 3 son independientes entre sí; 4 audita lo que 2 y 3 recibieron;
   5 consolida las tres salidas). El puntaje final del Consolidador se
   calcula en código (`app/domain/rules/scoring.py`), nunca por el LLM.

Sigue sin persistir nada en `factores`/`propuestas`/`ciclos_analisis` — eso
sigue siendo Paso 7. Solo `llamadas_ia` queda registrada (mismo registro de
auditoría que ya usa cualquier llamada a un agente).

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
from datetime import datetime, timezone
from decimal import Decimal

from app.ai.agents.auditor import run_auditor_datos
from app.ai.agents.consolidator import run_consolidador
from app.ai.agents.events import run_analista_eventos
from app.ai.agents.quantitative import run_analista_cuantitativo
from app.config import get_settings
from app.data.exchange_client import SUPPORTED_TIMEFRAMES
from app.data.indicator_calculator import (
    INDICATOR_NAME_ATR,
    INDICATOR_NAME_DISTANCIA_ATH,
    INDICATOR_NAME_DISTANCIA_MAX_90D,
    INDICATOR_NAME_RSI,
    INDICATOR_NAME_VOLUMEN_RELATIVO,
    IndicatorRow,
    SMA_PERIODS,
    sma_indicator_name,
)
from app.data.prefilter_pipeline import run_prefilter
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.symbols import ALLOWED_SYMBOLS
from app.logging_config import configure_logging
from app.repositories.candles import get_candles
from app.repositories.event_analysis import get_event_analysis_for_symbol
from app.repositories.events import get_events
from app.repositories.indicators import get_indicators

_DAILY_TIMEFRAME = "1d"


def _pick_symbol(session, requested: str | None) -> str:
    if requested:
        symbol = requested.upper()
        if symbol not in ALLOWED_SYMBOLS:
            raise SystemExit(
                f"'{symbol}' no está en la whitelist. Símbolos permitidos: {', '.join(ALLOWED_SYMBOLS)}."
            )
        return symbol

    result = run_prefilter(session)
    if not result.candidates:
        raise SystemExit(
            "El pre-filtro no encontró ningún candidato hoy y no se pasó --simbolo. "
            "Corre 'python -m scripts.run_prefilter' para ver por qué, o elige un símbolo a mano."
        )
    return result.candidates[0].symbol


def _latest(rows: list[IndicatorRow], indicator_name: str) -> IndicatorRow | None:
    """La fila MÁS RECIENTE (mayor `calculated_at`) de `indicator_name` en
    `rows`, o `None` si ese indicador nunca se calculó para este símbolo/
    temporalidad."""
    candidatas = [r for r in rows if r.indicator_name == indicator_name]
    if not candidatas:
        return None
    return max(candidatas, key=lambda r: r.calculated_at)


def _build_temporalidad_block(session, symbol: str, timeframe: str, faltantes: list[str]) -> dict:
    candles = get_candles(session, symbol, timeframe)
    if not candles:
        faltantes.append(f"velas_{timeframe}")
        return {"disponible": False}

    indicators = get_indicators(session, symbol, timeframe)
    last_close = candles[-1]["close"]
    last_open_time = candles[-1]["open_time"]

    def _valor_o_faltante(indicator_name: str, etiqueta: str):
        row = _latest(indicators, indicator_name)
        if row is None:
            faltantes.append(f"{etiqueta}_{timeframe}")
            return None
        return float(row.value)

    bloque: dict = {
        "precio_cierre_ultima_vela": float(last_close),
        "vela_mas_reciente_utc": last_open_time.isoformat(),
        "rsi_14": _valor_o_faltante(INDICATOR_NAME_RSI, "rsi_14"),
        "atr_14": _valor_o_faltante(INDICATOR_NAME_ATR, "atr_14"),
        "volumen_relativo_30": _valor_o_faltante(INDICATOR_NAME_VOLUMEN_RELATIVO, "volumen_relativo_30"),
    }

    for period in SMA_PERIODS:
        sma_row = _latest(indicators, sma_indicator_name(period))
        clave_sma = f"sma_{period}"
        clave_distancia = f"distancia_pct_sma_{period}"
        if sma_row is None or sma_row.value == 0:
            faltantes.append(f"{clave_sma}_{timeframe}")
            bloque[clave_sma] = None
            bloque[clave_distancia] = None
        else:
            bloque[clave_sma] = float(sma_row.value)
            bloque[clave_distancia] = float((last_close - sma_row.value) / sma_row.value * Decimal(100))

    # Distancia a ATH y a máximo de 90 días: solo se calculan en velas
    # diarias (sección 6.1 del plan) — en el resto de temporalidades no es
    # que falten, es que no aplican, así que no se reportan como faltantes.
    if timeframe == _DAILY_TIMEFRAME:
        for indicator_name, clave in (
            (INDICATOR_NAME_DISTANCIA_ATH, "distancia_pct_ath"),
            (INDICATOR_NAME_DISTANCIA_MAX_90D, "distancia_pct_max_90d"),
        ):
            row = _latest(indicators, indicator_name)
            if row is None:
                faltantes.append(clave)
                bloque[clave] = None
            else:
                bloque[clave] = float(row.value)

    return bloque


def build_datos_analista_cuantitativo(session, symbol: str) -> tuple[dict, list[str]]:
    """Bloque DATOS real del Agente 2 (sección 7.2 del plan: "json_indicadores").
    Devuelve también la lista de campos que no se pudieron completar, para
    pasársela después al Auditor (Agente 4)."""
    faltantes: list[str] = []
    temporalidades = {
        timeframe: _build_temporalidad_block(session, symbol, timeframe, faltantes)
        for timeframe in SUPPORTED_TIMEFRAMES
    }
    return {"simbolo": symbol, "temporalidades": temporalidades}, faltantes


def build_datos_analista_eventos(session, symbol: str) -> tuple[dict, list[str]]:
    """Bloque DATOS real del Agente 3 (sección 7.2 del plan: "json_correlaciones").

    No decide cuáles eventos son "comparables" — esa definición estadística
    sigue pendiente (sección 17 del plan, Fase 2). Solo entrega, para cada
    evento cuyo `scope` ya incluye a `symbol` (columna `analisis_eventos.symbol`,
    ya filtrada por alcance al calcularse — ver
    `app/repositories/event_analysis.py::get_event_analysis_for_symbol`), su
    descripción curada y el cambio porcentual de precio en cada ventana. Es
    el propio agente quien juzga semejanza, con esos datos y nada más."""
    eventos_por_id = {e.id: e for e in get_events(session)}
    filas = get_event_analysis_for_symbol(session, symbol)

    eventos_en_alcance: dict[str, dict] = {}
    for fila in filas:
        evento = eventos_por_id.get(fila.evento_id)
        if evento is None:
            continue
        entrada = eventos_en_alcance.setdefault(
            evento.external_id,
            {
                "evento_id": evento.external_id,
                "fecha": evento.event_date.isoformat(),
                "tipo": evento.event_type,
                "descripcion": evento.description,
                "severidad": evento.severity,
                "cambios_precio_pct_por_ventana_dias": {},
            },
        )
        entrada["cambios_precio_pct_por_ventana_dias"][str(fila.window_offset_days)] = float(
            fila.price_change_pct
        )

    datos = {"simbolo": symbol, "eventos_del_catalogo_en_alcance": list(eventos_en_alcance.values())}
    faltantes = [] if eventos_en_alcance else ["analisis_eventos_para_este_simbolo"]
    return datos, faltantes


def build_datos_auditor(
    session,
    symbol: str,
    faltantes_cuantitativo: list[str],
    faltantes_eventos: list[str],
    eventos_en_catalogo: int,
) -> dict:
    """Bloque DATOS real del Agente 4. El plan no fija una forma exacta de
    entrada para este agente (a diferencia de 2/3, que sí referencian
    "json_indicadores"/"json_correlaciones"); esta forma reutiliza la misma
    ya usada en `scripts/run_agent_demo.py`, ahora con valores reales."""
    daily_candles = get_candles(session, symbol, _DAILY_TIMEFRAME)
    velas_diarias_disponibles = len(daily_candles)

    if daily_candles:
        ultima_vela_hace_horas = (
            datetime.now(timezone.utc) - daily_candles[-1]["open_time"]
        ).total_seconds() / 3600
    else:
        ultima_vela_hace_horas = None

    # Continuidad: un conteo real de velas esperadas vs. disponibles entre la
    # primera y la última fecha (nunca una lista de fechas exactas inventada
    # — sección P3 del plan; esto es una señal agregada honesta, no un
    # detector preciso de huecos).
    huecos_en_series: list[str] = []
    if len(daily_candles) >= 2:
        dias_cubiertos = (daily_candles[-1]["open_time"] - daily_candles[0]["open_time"]).days
        esperadas = dias_cubiertos + 1
        if velas_diarias_disponibles < esperadas:
            huecos_en_series.append(
                f"velas_1d: se esperaban ~{esperadas} velas entre "
                f"{daily_candles[0]['open_time'].date()} y {daily_candles[-1]['open_time'].date()}, "
                f"hay {velas_diarias_disponibles} (posible hueco)"
            )

    return {
        "simbolo": symbol,
        "velas_diarias_disponibles": velas_diarias_disponibles,
        "ultima_vela_hace_horas": ultima_vela_hace_horas,
        "huecos_en_series_detectados": huecos_en_series,
        "campos_faltantes_reportados_por_analista_cuantitativo": faltantes_cuantitativo,
        "campos_faltantes_reportados_por_analista_eventos": faltantes_eventos,
        "eventos_del_catalogo_en_alcance_de_este_simbolo": eventos_en_catalogo,
    }


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
        symbol = _pick_symbol(session, args.simbolo)
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
                "(sección 7.2 del plan), un ciclo real se detendría aquí sin generar ninguna "
                "propuesta. Este demo sigue de todos modos hasta el Consolidador, para poder "
                "inspeccionar su comportamiento; la orquestación real (Paso 7) SÍ debe cortar "
                "el ciclo en este punto."
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
