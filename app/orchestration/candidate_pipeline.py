"""Arma, a partir de datos YA calculados en la base (nunca los recalcula:
ver `app.domain.enums.EstadoCiclo`), el bloque DATOS de cada agente para un
símbolo — sección 7.2 del plan ("json_indicadores", "json_correlaciones") y
Paso 7 ("orquestación... a partir de datos reales de la base").

Trasladado tal cual desde `scripts/run_agent_demo_full_cycle.py` (Paso 6),
donde se verificó primero contra la API real de Anthropic antes de
comprometerse a este código como parte permanente del orquestador
(`app/orchestration/daily_cycle.py`) — ese script ahora importa estas
funciones en vez de definirlas, para no tener dos copias divergentes.

Reglas que se repiten en las tres funciones (P3 del plan: cero datos
inventados):

- Un indicador o vela ausente nunca se sustituye por un valor por
  defecto: se reporta en una lista `faltantes`/`campos_faltantes...` y el
  campo queda en `None` en el bloque DATOS. Quien recibe ese bloque (el
  agente, o el Auditor a partir de las listas de faltantes de los otros
  dos) decide qué hacer con la ausencia — esta capa no decide nada, solo
  arma el bloque (sección `data` del plan: "adquisición... no ejecuta
  órdenes", y aquí tampoco decide).
- Ningún valor se calcula aquí: los indicadores ya vienen calculados por
  `app/data/indicator_calculator.py`, los cambios porcentuales de eventos
  por `app/data/event_analyzer.py`. Esta capa solo lee y da forma al JSON.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

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
from app.domain.rules.candles import hora_de_cierre
from app.domain.symbols import ALLOWED_SYMBOLS
from app.repositories.candles import get_candles
from app.repositories.event_analysis import get_event_analysis_for_symbol
from app.repositories.events import get_events
from app.repositories.indicators import get_indicators

_DAILY_TIMEFRAME = "1d"


def pick_symbol(session: Session, requested: str | None) -> str:
    """El símbolo a analizar: el pasado explícitamente (validado contra la
    whitelist), o el mejor candidato del pre-filtro determinista de hoy.

    Lanza `SystemExit` (no `ValueError`) cuando no hay ninguna forma de
    continuar sin inventar un símbolo — mismo criterio que ya usaba el
    script de humo del Paso 6, pensado para un punto de entrada de línea de
    comandos; `daily_cycle.py` lo captura como "ciclo sin candidatos", no
    como un error del programa.
    """
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
            "El pre-filtro no encontró ningún candidato hoy y no se pasó un símbolo explícito."
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


def _build_temporalidad_block(session: Session, symbol: str, timeframe: str, faltantes: list[str]) -> dict:
    candles = get_candles(session, symbol, timeframe)
    if not candles:
        faltantes.append(f"velas_{timeframe}")
        return {"disponible": False}

    last_close = candles[-1]["close"]
    last_open_time = candles[-1]["open_time"]
    # Solo indicadores calculados sobre velas YA CERRADAS: nada posterior a la
    # última vela cerrada puede entrar al análisis (sin look-ahead, sección 15).
    indicators = [r for r in get_indicators(session, symbol, timeframe) if r.calculated_at <= last_open_time]

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


def build_datos_analista_cuantitativo(session: Session, symbol: str) -> tuple[dict, list[str]]:
    """Bloque DATOS real del Agente 2 (sección 7.2 del plan: "json_indicadores").
    Devuelve también la lista de campos que no se pudieron completar, para
    pasársela después al Auditor (Agente 4)."""
    faltantes: list[str] = []
    temporalidades = {
        timeframe: _build_temporalidad_block(session, symbol, timeframe, faltantes)
        for timeframe in SUPPORTED_TIMEFRAMES
    }
    return {"simbolo": symbol, "temporalidades": temporalidades}, faltantes


def build_datos_analista_eventos(session: Session, symbol: str) -> tuple[dict, list[str]]:
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
    session: Session,
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
        # Antigüedad de la última vela CERRADA, medida desde su cierre.
        ultima_vela_hace_horas = (
            datetime.now(timezone.utc) - hora_de_cierre(daily_candles[-1]["open_time"], _DAILY_TIMEFRAME)
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
