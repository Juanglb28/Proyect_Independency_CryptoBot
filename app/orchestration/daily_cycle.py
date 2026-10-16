"""Orquestador del ciclo de análisis diario (Paso 7 del plan — sección
15.6, tramo de análisis; sección 4.2, "Flujo de un ciclo de análisis").

Lee velas/indicadores/eventos/`analisis_eventos` YA calculados en la base
(nunca los recalcula — ver el docstring de `app.domain.enums.EstadoCiclo`
sobre por qué INGEST/VALIDATE_DATA/CALCULATE_INDICATORS quedan fuera de
este paso), corre el pre-filtro determinista, arma el bloque DATOS de cada
candidato, llama a los 4 agentes no conversacionales en su orden real de
dependencia, calcula el score de cada opción del Consolidador en código, y
persiste TODO — ciclo, propuestas y factores — con trazabilidad completa.

## Reglas de corte

Ninguna es nueva: ya estaban decididas en pasos previos, o confirmadas
explícitamente con el usuario para este paso (2026-09-30).

1. Si el pre-filtro no encuentra candidatos, el ciclo termina sin ninguna
   llamada a la API (sección 4.2 del plan: "cero llamadas en días sin
   candidatos").
2. Si la fórmula determinista de calidad de datos
   (`app.domain.rules.data_quality`) detecta condiciones bloqueantes para
   un candidato (lista cerrada: sin velas diarias, etc.), ESE candidato
   queda excluido del bloque DATOS que recibe el Consolidador. El Auditor
   IA se mantiene como observador: su output se guarda en el snapshot para
   trazabilidad, pero ya no determina ni el score ni el bloqueo (decisión
   del usuario, 2026-10-08). Si TODOS los candidatos quedan excluidos, el
   Consolidador ni siquiera se llama.
3. Se persiste como `Propuesta` CADA opción que el Consolidador devuelve,
   incluidas las que no superan `SCORE_MINIMO_ENTRADA` — trazabilidad
   completa; decidir cuál se convierte en orden paper es Paso 8.
4. `decision_snapshot` NO incluye `initial_stop`/`initial_target` todavía
   — no existe `app/domain/rules/risk.py` (TODO explícito, Paso 8).

## Manejo de errores y P5 ("el sistema debe poder morir y revivir")

Cada llamada real a un agente cuesta dinero — perder ese registro por un
error posterior sería repetir el bug real ya encontrado en esta sesión
(una transacción fallida se llevó por delante el resultado de una llamada
que sí había funcionado). Por eso se hace `session.commit()` después de
cada bloque de costo real (creación del ciclo, cada candidato evaluado, la
llamada al Consolidador, cada propuesta persistida) — mismo patrón que
`app.data.candle_ingestion.backfill_all`.

Un error no manejado en cualquier punto deja el ciclo con `error_message`
no nulo y `status` = la última etapa alcanzada (sección 15.6: "un fallo en
una etapa no debe permitir saltar silenciosamente a una etapa
posterior") — nunca se atrapa el error y se continúa como si nada; la
excepción se relanza después de registrar el fallo.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.ai.agents.auditor import run_auditor_datos
from app.ai.agents.consolidator import run_consolidador
from app.ai.agents.events import run_analista_eventos
from app.ai.agents.quantitative import run_analista_cuantitativo
from app.ai.models import MODEL_ANALISTA, MODEL_AUDITOR, MODEL_CONSOLIDADOR
from app.ai.prompts.auditor import PROMPT_VERSION as PROMPT_VERSION_AUDITOR
from app.ai.prompts.consolidator import PROMPT_VERSION as PROMPT_VERSION_CONSOLIDADOR
from app.ai.prompts.events import PROMPT_VERSION as PROMPT_VERSION_EVENTOS
from app.ai.prompts.quantitative import PROMPT_VERSION as PROMPT_VERSION_CUANTITATIVO
from app.config import Settings
from app.data.prefilter import PREFILTER_FORMULA_VERSION, clasificar_resultados
from app.data.indicator_calculator import SMA_PERIODS
from app.domain.rules.data_quality import (
    DATA_QUALITY_FORMULA_VERSION,
    DataQualityResult,
    calculate_data_quality,
)
from app.domain.rules.quantitative_score import (
    QUANTITATIVE_FORMULA_VERSION,
    QuantitativeScore,
    calculate_quantitative_score,
)
from app.data.prefilter_pipeline import run_prefilter
from app.data.relative_strength import calculate_ratio_rsi
from app.domain.enums import EstadoCiclo
from app.domain.rules.entry_tier import (
    clasificar_tier,
    contar_ciclos_consecutivos_en_zona,
    en_zona_segunda_ronda,
    evaluar_segunda_ronda,
)
from app.domain.rules.scoring import (
    PESO_CONTEXTO_EVENTOS,
    PESO_POSICION_RANGOS_ATH,
    PESO_TENDENCIA_MOMENTUM,
    PESO_VOLUMEN_LIQUIDEZ,
)
from app.orchestration.candidate_pipeline import (
    build_datos_analista_cuantitativo,
    build_datos_analista_eventos,
    build_datos_auditor,
)
from app.repositories.analysis import (
    FactorRow,
    PrefiltroRow,
    PropuestaRow,
    avanzar_ciclo,
    create_ciclo,
    finalizar_ciclo,
    get_ciclo,
    get_puntajes_de_ciclos_previos,
    save_prefiltro_resultados,
    save_propuesta,
)
from app.repositories.candles import get_candles

logger = logging.getLogger(__name__)

_PESO_POR_FACTOR: dict[str, Decimal] = {
    "tendencia_momentum": PESO_TENDENCIA_MOMENTUM,
    "volumen_liquidez": PESO_VOLUMEN_LIQUIDEZ,
    "posicion_rangos_ath": PESO_POSICION_RANGOS_ATH,
    "contexto_eventos": PESO_CONTEXTO_EVENTOS,
}

_MODEL_VERSIONS = {
    "analista_cuantitativo": MODEL_ANALISTA,
    "analista_eventos": MODEL_ANALISTA,
    "auditor_datos": MODEL_AUDITOR,
    "consolidador": MODEL_CONSOLIDADOR,
}
_PROMPT_VERSIONS = {
    "analista_cuantitativo": PROMPT_VERSION_CUANTITATIVO,
    "analista_eventos": PROMPT_VERSION_EVENTOS,
    "auditor_datos": PROMPT_VERSION_AUDITOR,
    "consolidador": PROMPT_VERSION_CONSOLIDADOR,
}


@dataclass(frozen=True)
class _DatosCandidato:
    """Todo lo que se arma y se necesita para un candidato, de principio a
    fin del ciclo — evita recalcular o releer nada dos veces."""

    symbol: str
    datos_cuantitativo: dict
    datos_eventos: dict
    faltantes_cuantitativo: list[str]
    faltantes_eventos: list[str]
    analista_cuantitativo: dict | None = None
    analista_eventos: dict | None = None
    auditoria_datos: dict | None = None
    calidad_datos: Decimal | None = None
    data_quality: DataQualityResult | None = None
    """Resultado de la fórmula determinista de calidad de datos (reemplaza
    a `100 - auditor.penalizacion_confianza_pct` como fuente de
    `calidad_datos`). `auditoria_datos` se mantiene como observador: su
    output se guarda en el snapshot para comparación, pero ya no afecta el
    score ni la decisión de bloqueo."""


@dataclass(frozen=True)
class DailyCycleResult:
    ciclo_id: uuid.UUID
    candidatos_evaluados: tuple[str, ...]
    candidatos_excluidos_por_calidad: tuple[str, ...]
    propuesta_ids: tuple[uuid.UUID, ...]
    cost_usd: Decimal
    # Lo que respondió el Consolidador, tal cual — `None` si el ciclo ni
    # siquiera llegó a llamarlo (sin candidatos del pre-filtro, o todos
    # excluidos por calidad de datos). Se expone para que quien imprima el
    # resultado pueda explicar un "sin propuestas" sin consultar la base de
    # datos (BUG/OBSERVACIÓN 2026-10-04: la salida solo decía "Sin
    # propuestas este ciclo." sin distinguir las causas posibles). Es un
    # dato de solo lectura copiado de la respuesta ya validada — no se
    # recalcula ni se interpreta aquí (P1).
    consolidador_recomendacion: str | None = None
    consolidador_razon_no_invertir: str | None = None
    consolidador_confianza_global: str | None = None


def _marcar_fallo_y_relanzar(session: Session, ciclo_id: uuid.UUID, costo_total: Decimal, exc: Exception) -> None:
    ciclo = get_ciclo(session, ciclo_id)
    ultima_etapa = ciclo.status if ciclo is not None else EstadoCiclo.PREFILTER
    logger.exception("Ciclo %s falló en la etapa %s", ciclo_id, ultima_etapa)
    finalizar_ciclo(
        session,
        ciclo_id,
        status=ultima_etapa,
        finished_at=datetime.now(timezone.utc),
        cost_usd=costo_total,
        error_message=str(exc),
    )
    session.commit()


def guardar_resultado_prefiltro(session: Session, ciclo_id: uuid.UUID, resultado) -> None:
    """Persiste el resultado del pre-filtro de este ciclo (`prefiltro_resultados`).
    Solo traduce `ResultadoPrefiltro` a filas; no calcula nada."""
    filas = []
    for r in clasificar_resultados(
        list(resultado.scores), list(resultado.candidates), list(resultado.symbols_sin_datos)
    ):
        sc = r.score
        filas.append(
            PrefiltroRow(
                symbol=r.symbol,
                estado=r.estado,
                ranking=r.ranking,
                calculated_at=None if sc is None else sc.calculated_at,
                tendencia_pct=None if sc is None else sc.tendencia_pct,
                volumen_relativo=None if sc is None else sc.volumen_relativo,
                distancia_max_90d_pct=None if sc is None else sc.distancia_max_90d_pct,
                score_tendencia=None if sc is None else sc.score_tendencia,
                score_volumen=None if sc is None else sc.score_volumen,
                score_distancia=None if sc is None else sc.score_distancia,
                score_final=None if sc is None else sc.score_final,
            )
        )
    save_prefiltro_resultados(session, ciclo_id, filas, formula_version=PREFILTER_FORMULA_VERSION)


def run_daily_cycle(session: Session, settings: Settings, *, now: datetime | None = None) -> DailyCycleResult:
    """Corre un ciclo de análisis completo y devuelve un resumen.

    No conoce Streamlit ni Binance (sección `orchestration` del plan:
    "coordina servicios... no debe convertirse en un lugar donde se
    escondan cálculos financieros") — el único cálculo que hace es
    invocar `app.domain.rules.scoring.calculate_consolidated_score`
    (dentro de `run_consolidador`), nunca aritmética propia sobre dinero.
    """
    started_at = now or datetime.now(timezone.utc)
    ciclo_id = create_ciclo(session, started_at=started_at, status=EstadoCiclo.PREFILTER)
    session.commit()

    costo_total = Decimal(0)

    try:
        resultado_prefiltro = run_prefilter(session)
        candidatos = [c.symbol for c in resultado_prefiltro.candidates]

        # Evidencia de lo que el pre-filtro hizo este ciclo (incluidos los
        # descartados y los sin datos), guardada ANTES de seguir: así queda
        # aunque el ciclo termine aquí por falta de candidatos.
        guardar_resultado_prefiltro(session, ciclo_id, resultado_prefiltro)
        session.commit()

        if not candidatos:
            finalizar_ciclo(
                session,
                ciclo_id,
                status=EstadoCiclo.PREFILTER,
                finished_at=datetime.now(timezone.utc),
                cost_usd=costo_total,
                error_message=None,
            )
            session.commit()
            return DailyCycleResult(
                ciclo_id=ciclo_id,
                candidatos_evaluados=(),
                candidatos_excluidos_por_calidad=(),
                propuesta_ids=(),
                cost_usd=costo_total,
            )

        # --- EVENT_CORRELATION: arma el bloque DATOS de cada candidato ---
        avanzar_ciclo(session, ciclo_id, EstadoCiclo.EVENT_CORRELATION)
        session.commit()

        datos_candidatos: dict[str, _DatosCandidato] = {}
        for symbol in candidatos:
            datos_cuant, faltantes_cuant = build_datos_analista_cuantitativo(session, symbol)
            datos_evt, faltantes_evt = build_datos_analista_eventos(session, symbol)
            datos_candidatos[symbol] = _DatosCandidato(
                symbol=symbol,
                datos_cuantitativo=datos_cuant,
                datos_eventos=datos_evt,
                faltantes_cuantitativo=faltantes_cuant,
                faltantes_eventos=faltantes_evt,
            )

        # --- AI_ANALYSIS: agentes 2/3/4 por candidato ---
        avanzar_ciclo(session, ciclo_id, EstadoCiclo.AI_ANALYSIS)
        session.commit()

        candidatos_para_consolidador: list[dict] = []
        calidad_datos_por_simbolo: dict[str, Decimal] = {}
        excluidos: list[str] = []

        for symbol in candidatos:
            info = datos_candidatos[symbol]

            r2 = run_analista_cuantitativo(session, settings, info.datos_cuantitativo, ciclo_id=ciclo_id)
            costo_total += r2.cost_usd
            r3 = run_analista_eventos(session, settings, info.datos_eventos, ciclo_id=ciclo_id)
            costo_total += r3.cost_usd

            # Auditor IA: se mantiene como OBSERVADOR — su output se guarda
            # en el decision_snapshot para comparación y trazabilidad, pero
            # ya no afecta ni el score ni la decisión de bloqueo (decisión
            # del usuario, 2026-10-08: reemplazar con fórmula determinista).
            datos_aud = build_datos_auditor(
                session,
                symbol,
                info.faltantes_cuantitativo,
                info.faltantes_eventos,
                len(info.datos_eventos["eventos_del_catalogo_en_alcance"]),
            )
            r4 = run_auditor_datos(session, settings, datos_aud, ciclo_id=ciclo_id)
            costo_total += r4.cost_usd
            session.commit()  # 3 llamadas reales ya pagadas para este candidato

            # Calidad de datos DETERMINISTA: reemplaza la opinión subjetiva
            # del auditor IA (`penalizacion_confianza_pct`) como fuente de
            # `calidad_datos` para el multiplicador del score. Usa los mismos
            # inputs que ya se prepararon para el auditor.
            dq = calculate_data_quality(
                campos_faltantes_cuantitativo=info.faltantes_cuantitativo,
                campos_faltantes_eventos=info.faltantes_eventos,
                huecos_en_series=datos_aud["huecos_en_series_detectados"],
                ultima_vela_hace_horas=datos_aud["ultima_vela_hace_horas"],
                velas_diarias_disponibles=datos_aud["velas_diarias_disponibles"],
                eventos_en_catalogo=datos_aud["eventos_del_catalogo_en_alcance_de_este_simbolo"],
            )

            datos_candidatos[symbol] = _DatosCandidato(
                **{
                    **info.__dict__,
                    "analista_cuantitativo": r2.output.model_dump(),
                    "analista_eventos": r3.output.model_dump(),
                    "auditoria_datos": r4.output.model_dump(),
                    "calidad_datos": dq.calidad_datos,
                    "data_quality": dq,
                }
            )

            # Bloqueo determinista: lista cerrada de condiciones que impiden
            # generar una propuesta (reemplaza `calidad_global == "insuficiente"`).
            if dq.bloqueantes:
                logger.info(
                    "Candidato %s excluido por bloqueantes deterministas: %s",
                    symbol,
                    dq.bloqueantes,
                )
                excluidos.append(symbol)
                continue

            if dq.presenta_anomalias:
                logger.info(
                    "Candidato %s presenta anomalías (informativo, sin penalización): %s",
                    symbol,
                    dq.anomalias,
                )

            bloque_consolidador: dict = {
                "simbolo": symbol,
                "analisis_cuantitativo": r2.output.model_dump(),
                "analisis_eventos": r3.output.model_dump(),
                "auditoria_datos": r4.output.model_dump(),
            }
            # Informar al Consolidador de las anomalías detectadas para que
            # pueda mencionarlas en su tesis si lo considera relevante — pero
            # esto es CONTEXTO, no una instrucción de penalizar.
            if dq.presenta_anomalias:
                bloque_consolidador["anomalias_datos_detectadas"] = list(dq.anomalias)

            candidatos_para_consolidador.append(bloque_consolidador)
            calidad_datos_por_simbolo[symbol] = dq.calidad_datos

        if not candidatos_para_consolidador:
            finalizar_ciclo(
                session,
                ciclo_id,
                status=EstadoCiclo.AI_ANALYSIS,
                finished_at=datetime.now(timezone.utc),
                cost_usd=costo_total,
                error_message=None,
            )
            session.commit()
            return DailyCycleResult(
                ciclo_id=ciclo_id,
                candidatos_evaluados=tuple(candidatos),
                candidatos_excluidos_por_calidad=tuple(excluidos),
                propuesta_ids=(),
                cost_usd=costo_total,
            )

        # --- CONSOLIDATE ---
        avanzar_ciclo(session, ciclo_id, EstadoCiclo.CONSOLIDATE)
        session.commit()

        r5 = run_consolidador(
            session,
            settings,
            {"candidatos": candidatos_para_consolidador},
            calidad_datos_por_simbolo,
            ciclo_id=ciclo_id,
        )
        costo_total += r5.agent_result.cost_usd
        session.commit()

        # --- FREEZE_DECISION_SNAPSHOT ---
        avanzar_ciclo(session, ciclo_id, EstadoCiclo.FREEZE_DECISION_SNAPSHOT)
        session.commit()

        propuesta_ids: list[uuid.UUID] = []
        congelado_en = datetime.now(timezone.utc)
        for evaluada in r5.opciones_evaluadas:
            propuesta_id = _congelar_y_guardar_propuesta(
                session,
                ciclo_id=ciclo_id,
                opcion=evaluada.opcion,
                score=evaluada.score,
                candidato=datos_candidatos[evaluada.opcion.simbolo],
                confianza_global_consolidador=r5.output.confianza_global,
                congelado_en=congelado_en,
            )
            propuesta_ids.append(propuesta_id)
            session.commit()

        finalizar_ciclo(
            session,
            ciclo_id,
            status=EstadoCiclo.FREEZE_DECISION_SNAPSHOT,
            finished_at=datetime.now(timezone.utc),
            cost_usd=costo_total,
            error_message=None,
        )
        session.commit()

        return DailyCycleResult(
            ciclo_id=ciclo_id,
            candidatos_evaluados=tuple(candidatos),
            candidatos_excluidos_por_calidad=tuple(excluidos),
            propuesta_ids=tuple(propuesta_ids),
            cost_usd=costo_total,
            consolidador_recomendacion=r5.output.recomendacion,
            consolidador_razon_no_invertir=r5.output.razon_no_invertir,
            consolidador_confianza_global=r5.output.confianza_global,
        )

    except Exception as exc:  # noqa: BLE001 — se relanza; ver docstring del módulo
        _marcar_fallo_y_relanzar(session, ciclo_id, costo_total, exc)
        raise


_SIMBOLO_REFERENCIA_RELATIVA = "BTCUSDT"
_TEMPORALIDAD_DIARIA_ROUND2 = "1d"


def _evaluar_segunda_ronda_si_aplica(session: Session, *, symbol: str, score, candidato: _DatosCandidato):
    """Segunda ronda de entradas de mayor riesgo (`app/domain/rules/
    entry_tier.py`, decisión del usuario 2026-10-04). Devuelve `None` si el
    puntaje no cae en la zona `[50, 55)` — en ese caso no se evalúa nada.

    Los RSI por temporalidad salen del mismo bloque DATOS ya armado para el
    Analista cuantitativo (los mismos valores que quedan en el
    `decision_snapshot`); el RSI del ratio contra BTC se calcula en código
    (`app/data/relative_strength.py`) con las velas diarias ya guardadas."""
    if not en_zona_segunda_ronda(score.puntaje_ponderado):
        return None

    temporalidades = candidato.datos_cuantitativo["temporalidades"]
    rsi_por_tf = {
        tf: (None if bloque.get("rsi_14") is None else Decimal(str(bloque["rsi_14"])))
        for tf, bloque in temporalidades.items()
    }

    if symbol == _SIMBOLO_REFERENCIA_RELATIVA:
        rsi_ratio = None  # contra sí mismo no define fuerza relativa
    else:
        rsi_ratio = calculate_ratio_rsi(
            get_candles(session, symbol, _TEMPORALIDAD_DIARIA_ROUND2),
            get_candles(session, _SIMBOLO_REFERENCIA_RELATIVA, _TEMPORALIDAD_DIARIA_ROUND2),
        )

    return evaluar_segunda_ronda(rsi_por_temporalidad=rsi_por_tf, rsi_ratio_btc_diario=rsi_ratio)


def _calcular_score_cuantitativo_paralelo(
    candidato: _DatosCandidato,
    calculated_at: datetime,
) -> QuantitativeScore | None:
    """Extrae los indicadores diarios del bloque DATOS del candidato y
    calcula el score cuantitativo paralelo (Fase B — shadow scoring).

    Devuelve `None` si faltan datos diarios o algún indicador — misma
    filosofía P3 que el resto del sistema.

    Este score NO decide nada: se guarda en el `decision_snapshot` para
    comparación futura con el score del Consolidador (IA).
    """
    temporalidades = candidato.datos_cuantitativo.get("temporalidades", {})
    bloque_diario = temporalidades.get("1d")
    if bloque_diario is None:
        return None

    # Extraer distancias %-vs-SMA para cada periodo (90, 120, 180)
    sma_pct_values: list[Decimal | None] = []
    for period in SMA_PERIODS:
        raw = bloque_diario.get(f"distancia_pct_sma_{period}")
        sma_pct_values.append(None if raw is None else Decimal(str(raw)))

    # Volumen relativo
    raw_vol = bloque_diario.get("volumen_relativo_30")
    volumen_relativo = None if raw_vol is None else Decimal(str(raw_vol))

    # Distancia al máximo de 90 días
    raw_dist = bloque_diario.get("distancia_pct_max_90d")
    distancia_max_90d_pct = None if raw_dist is None else Decimal(str(raw_dist))

    # RSI 14
    raw_rsi = bloque_diario.get("rsi_14")
    rsi = None if raw_rsi is None else Decimal(str(raw_rsi))

    return calculate_quantitative_score(
        candidato.symbol,
        calculated_at,
        sma_pct_values=sma_pct_values,
        volumen_relativo=volumen_relativo,
        distancia_max_90d_pct=distancia_max_90d_pct,
        rsi=rsi,
    )


def _congelar_y_guardar_propuesta(
    session: Session,
    *,
    ciclo_id: uuid.UUID,
    opcion,  # OpcionConsolidada
    score,  # ScoreConsolidado
    candidato: _DatosCandidato,
    confianza_global_consolidador: str,
    congelado_en: datetime,
) -> uuid.UUID:
    """Congela el `decision_snapshot` (sección 8.3 del plan) de una opción y
    la persiste junto con sus 4 factores.

    Mapeo de los campos de la sección 8.3 a lo que el sistema realmente
    tiene hoy (nada se inventa para llenar un nombre de campo):
    - "precio de referencia", RSI/medias/volumen/ATR/distancias: vienen tal
      cual del bloque DATOS ya armado del Analista cuantitativo
      (`indicadores_por_temporalidad`), junto con la interpretación que ese
      agente hizo de esos datos (`analisis_cuantitativo`).
    - "contexto de eventos": el bloque DATOS ya armado del Analista de
      eventos, junto con su interpretación (`analisis_eventos`).
    - "opportunity/risk/confidence scores": `puntaje_ponderado` (el único
      score numérico que produce el sistema), `nivel_riesgo` (cualitativo,
      juicio del Consolidador para esta opción) y `confianza_global`
      (cualitativo, juicio del Consolidador sobre el conjunto — no existen
      tres scores numéricos independientes).
    - "contexto de mercado": se cubre con `auditoria_datos` (la salida
      completa del Auditor), que es la evaluación de qué tan confiables
      eran esos datos en el momento de la decisión — relevante para el
      análisis de look-ahead bias (sección 15: "¿qué sabía exactamente el
      sistema en el instante en que tomó esta decisión?").
    - "versión de modelo"/"versión de prompt": una por cada uno de los 4
      agentes que participaron.
    - "stop inicial"/"target inicial": deliberadamente AUSENTES — ver el
      docstring de `app.db.models.analysis.Propuesta` (TODO, Paso 8).
    """
    segunda_ronda = _evaluar_segunda_ronda_si_aplica(
        session, symbol=opcion.simbolo, score=score, candidato=candidato
    )
    tier_entrada = clasificar_tier(
        puntaje_ponderado=score.puntaje_ponderado,
        supera_umbral_entrada=score.supera_umbral_entrada,
        segunda_ronda=segunda_ronda,
    )

    # Observación pedida por el usuario (2026-10-04): cuántos días seguidos
    # lleva este activo cerca del umbral. NO decide ni cambia el tier.
    ciclos_consecutivos_en_zona = contar_ciclos_consecutivos_en_zona(
        score.puntaje_ponderado,
        get_puntajes_de_ciclos_previos(session, opcion.simbolo, ciclo_id),
    )

    # Score cuantitativo PARALELO (Fase B — shadow scoring): se calcula
    # en código, sin componente de IA, usando los mismos indicadores diarios
    # que ya están en el bloque DATOS. Se guarda en el snapshot como
    # INFORMACIÓN para la futura validación walk-forward (Fase C), NUNCA
    # como decisión — el score del Consolidador sigue siendo el que decide.
    score_cuantitativo = _calcular_score_cuantitativo_paralelo(
        candidato, congelado_en,
    )

    dq = candidato.data_quality
    decision_snapshot = {
        "symbol": opcion.simbolo,
        "analysis_cycle_id": str(ciclo_id),
        "captured_at": congelado_en.isoformat(),
        "indicadores_por_temporalidad": candidato.datos_cuantitativo["temporalidades"],
        "analisis_cuantitativo": candidato.analista_cuantitativo,
        "contexto_eventos": candidato.datos_eventos["eventos_del_catalogo_en_alcance"],
        "analisis_eventos": candidato.analista_eventos,
        # El auditor IA se mantiene como OBSERVADOR: su output queda en el
        # snapshot para trazabilidad y comparación futura con la fórmula
        # determinista, pero ya no determina el score ni el bloqueo.
        "auditoria_datos_ia": candidato.auditoria_datos,
        # Calidad de datos DETERMINISTA: la fuente real del multiplicador.
        "calidad_datos_determinista": {
            "calidad_datos": str(dq.calidad_datos) if dq else None,
            "penalizacion_campos": str(dq.penalizacion_campos) if dq else None,
            "penalizacion_antiguedad": str(dq.penalizacion_antiguedad) if dq else None,
            "penalizacion_huecos": str(dq.penalizacion_huecos) if dq else None,
            "presenta_anomalias": dq.presenta_anomalias if dq else None,
            "anomalias": list(dq.anomalias) if dq else [],
            "formula_version": dq.formula_version if dq else None,
        },
        # Score cuantitativo paralelo (Fase B): informativo, no decisorio.
        "score_cuantitativo_paralelo": {
            "score_final": str(score_cuantitativo.score_final) if score_cuantitativo else None,
            "score_tendencia": str(score_cuantitativo.score_tendencia) if score_cuantitativo else None,
            "score_volumen": str(score_cuantitativo.score_volumen) if score_cuantitativo else None,
            "score_distancia": str(score_cuantitativo.score_distancia) if score_cuantitativo else None,
            "score_rsi": str(score_cuantitativo.score_rsi) if score_cuantitativo else None,
            "tendencia_pct": str(score_cuantitativo.tendencia_pct) if score_cuantitativo else None,
            "volumen_relativo": str(score_cuantitativo.volumen_relativo) if score_cuantitativo else None,
            "distancia_max_90d_pct": str(score_cuantitativo.distancia_max_90d_pct) if score_cuantitativo else None,
            "rsi": str(score_cuantitativo.rsi) if score_cuantitativo else None,
            "formula_version": score_cuantitativo.formula_version if score_cuantitativo else None,
        },
        "datos_faltantes": {
            "cuantitativo": candidato.faltantes_cuantitativo,
            "eventos": candidato.faltantes_eventos,
        },
        "tier_entrada": tier_entrada.value,
        "ciclos_consecutivos_en_zona": ciclos_consecutivos_en_zona,
        "segunda_ronda": None if segunda_ronda is None else segunda_ronda.to_dict(),
        "opportunity_score": str(score.puntaje_ponderado),
        "risk_level": opcion.nivel_riesgo,
        "confidence": confianza_global_consolidador,
        "thesis": opcion.tesis,
        "invalidation": opcion.invalidacion,
        "factor_snapshot": [
            {"factor": nombre, "peso": str(peso), "puntaje": str(getattr(opcion, nombre))}
            for nombre, peso in _PESO_POR_FACTOR.items()
        ],
        "model_versions": _MODEL_VERSIONS,
        "prompt_versions": _PROMPT_VERSIONS,
    }

    factores = tuple(
        FactorRow(
            symbol=opcion.simbolo,
            factor=nombre,
            peso=peso,
            puntaje=Decimal(str(getattr(opcion, nombre))),
            confianza=confianza_global_consolidador,
        )
        for nombre, peso in _PESO_POR_FACTOR.items()
    )

    return save_propuesta(
        session,
        PropuestaRow(
            ciclo_id=ciclo_id,
            symbol=opcion.simbolo,
            categoria=opcion.categoria,
            horizonte=opcion.horizonte,
            nivel_riesgo=opcion.nivel_riesgo,
            puntaje_base=score.puntaje_base,
            calidad_datos=score.calidad_datos,
            puntaje_ponderado=score.puntaje_ponderado,
            supera_umbral_entrada=score.supera_umbral_entrada,
            tesis=opcion.tesis,
            invalidacion=opcion.invalidacion,
            escenario_adverso=opcion.escenario_adverso.model_dump(),
            comentario_chartista=opcion.comentario_chartista,
            fuentes=list(opcion.fuentes),
            confianza_global=confianza_global_consolidador,
            decision_snapshot=decision_snapshot,
            factores=factores,
            tier_entrada=tier_entrada,
            segunda_ronda=None if segunda_ronda is None else segunda_ronda.to_dict(),
            ciclos_consecutivos_en_zona=ciclos_consecutivos_en_zona,
        ),
    )
