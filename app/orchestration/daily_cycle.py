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
2. Si el Auditor marca `calidad_global="insuficiente"` para un candidato,
   ESE candidato queda excluido del bloque DATOS que recibe el
   Consolidador — nunca se le pide invertir sobre datos que su propio
   auditor declaró insuficientes (comentario ya dejado en
   `scripts/run_agent_demo_full_cycle.py` durante el Paso 6, al construir
   este paso). Si TODOS los candidatos quedan excluidos, el Consolidador
   ni siquiera se llama.
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
    PropuestaRow,
    avanzar_ciclo,
    create_ciclo,
    finalizar_ciclo,
    get_ciclo,
    get_puntajes_de_ciclos_previos,
    save_propuesta,
)
from app.repositories.candles import get_candles

logger = logging.getLogger(__name__)

AUDITOR_CALIDAD_INSUFICIENTE = "insuficiente"

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


@dataclass(frozen=True)
class DailyCycleResult:
    ciclo_id: uuid.UUID
    candidatos_evaluados: tuple[str, ...]
    candidatos_excluidos_por_calidad: tuple[str, ...]
    propuesta_ids: tuple[uuid.UUID, ...]
    cost_usd: Decimal


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

            datos_candidatos[symbol] = _DatosCandidato(
                **{
                    **info.__dict__,
                    "analista_cuantitativo": r2.output.model_dump(),
                    "analista_eventos": r3.output.model_dump(),
                    "auditoria_datos": r4.output.model_dump(),
                    "calidad_datos": Decimal(100) - Decimal(str(r4.output.penalizacion_confianza_pct)),
                }
            )

            if r4.output.calidad_global == AUDITOR_CALIDAD_INSUFICIENTE:
                excluidos.append(symbol)
                continue

            candidatos_para_consolidador.append(
                {
                    "simbolo": symbol,
                    "analisis_cuantitativo": r2.output.model_dump(),
                    "analisis_eventos": r3.output.model_dump(),
                    "auditoria_datos": r4.output.model_dump(),
                }
            )
            calidad_datos_por_simbolo[symbol] = datos_candidatos[symbol].calidad_datos

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

    decision_snapshot = {
        "symbol": opcion.simbolo,
        "analysis_cycle_id": str(ciclo_id),
        "captured_at": congelado_en.isoformat(),
        "indicadores_por_temporalidad": candidato.datos_cuantitativo["temporalidades"],
        "analisis_cuantitativo": candidato.analista_cuantitativo,
        "contexto_eventos": candidato.datos_eventos["eventos_del_catalogo_en_alcance"],
        "analisis_eventos": candidato.analista_eventos,
        "auditoria_datos": candidato.auditoria_datos,
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
