"""Operaciones de OBSERVACIÓN (decisión del usuario, 2026-10-12).

## Por qué existen

Hasta el 2026-10-12 ninguna posición paper se había abierto: la estrategia
exige puntaje ponderado >= 55 (o la zona de mayor riesgo) y el Consolidador casi
siempre recomienda no invertir, así que las corridas costaban dinero de API y no
producían operaciones. La v1 existe para MEDIR si el score predice, y eso exige
operaciones cerradas con una variedad de puntajes (el plan pide >= 30). Por eso,
cada candidato del pre-filtro (puntaje >= 45, estado `CANDIDATO`) que NO abrió
una entrada de la estrategia abre una posición paper de observación.

## Qué son (y qué no)

- NO son propuestas de la estrategia ni pasan por IA (P2): se arman en código
  desde el resultado del pre-filtro que ya está guardado en
  `prefiltro_resultados`. El tier `OBSERVACION` las distingue.
- Su "score" es el puntaje del PRE-FILTRO (escala 0-100, determinista), no el
  puntaje ponderado del Consolidador: son escalas distintas, por eso se miden
  como SERIE APARTE y nunca se mezclan con la de la estrategia al validar.
- Tamaño fijo de 100 USD, sin aprobación manual, cupos propios (20 abiertas y 5
  por día desde el 2026-10-26; antes 4 y 3), fuera del equity y de la pausa por drawdown de la estrategia.
  Salen por las mismas reglas del monitor (stop -8 %, TP +12 % con trailing).

## Trazabilidad (P3, sección 14 del master prompt)

La fila de `propuestas` de una observación NO inventa análisis: la tesis y la
invalidación son textos fijos que dicen que no hay análisis de IA, no hay
factores ni fuentes, y la calidad de datos queda en NULL (no se audita). El
`decision_snapshot` congela exactamente lo que el pre-filtro sabía en ese ciclo
(componentes del puntaje, versión de la fórmula, vela diaria base). Una vez
creada, no se modifica.

## Idempotencia

El id de la propuesta es DETERMINISTA (uuid5 de ciclo + símbolo): reintentar el
paso de apertura del mismo ciclo (p. ej. tras un reinicio) no crea una segunda
propuesta ni, por tanto, una segunda posición.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from app.domain.enums import EstadoPrefiltro

NAMESPACE_OBSERVACION = uuid.UUID("5d0b6a1e-7c3f-4b8a-9e21-0c4f6a7b8d93")
"""Namespace FIJO para los ids deterministas de observación. No cambiarlo: los
ids ya guardados dejarían de coincidir y un reintento duplicaría."""

CATEGORIA_OBSERVACION = "observacion"
HORIZONTE_OBSERVACION = "no_aplica"
NIVEL_RIESGO_OBSERVACION = "no_aplica"
TESIS_OBSERVACION = (
    "Operación de observación: candidato del pre-filtro determinista (puntaje >= 45) "
    "sin entrada de la estrategia. No hay análisis de IA."
)
INVALIDACION_OBSERVACION = (
    "No aplica: sin tesis de IA. La salida la deciden solo el stop, el objetivo y el trailing del monitor."
)
CONFIANZA_OBSERVACION = "no_aplica"
ORIGEN_SNAPSHOT_OBSERVACION = "OBSERVACION_DETERMINISTA"

_DOS_DECIMALES = Decimal("0.01")


@dataclass(frozen=True)
class DatosPrefiltro:
    """Lo que el pre-filtro guardó de un símbolo en un ciclo (copia de una fila
    de `prefiltro_resultados`, sin tocar la base)."""

    symbol: str
    estado: EstadoPrefiltro
    ranking: int | None
    calculated_at: datetime | None
    tendencia_pct: Decimal | None
    volumen_relativo: Decimal | None
    distancia_max_90d_pct: Decimal | None
    score_tendencia: Decimal | None
    score_volumen: Decimal | None
    score_distancia: Decimal | None
    score_final: Decimal | None
    formula_version: str


@dataclass(frozen=True)
class PropuestaDeObservacion:
    """Valores listos para guardar la fila de `propuestas` de una observación."""

    id: uuid.UUID
    ciclo_id: uuid.UUID
    symbol: str
    puntaje: Decimal
    """Puntaje del pre-filtro con 2 decimales (la columna es `Numeric(6, 2)`);
    el valor completo queda en el snapshot."""
    tesis: str
    invalidacion: str
    decision_snapshot: dict


def observation_proposal_id(ciclo_id: uuid.UUID, symbol: str) -> uuid.UUID:
    """Id determinista de la observación de `symbol` en el ciclo `ciclo_id`."""
    return uuid.uuid5(NAMESPACE_OBSERVACION, f"{ciclo_id}:{symbol}")


def _texto(valor: Decimal | None) -> str | None:
    return None if valor is None else str(valor)


def build_observation_proposal(ciclo_id: uuid.UUID, datos: DatosPrefiltro) -> PropuestaDeObservacion:
    """Arma la propuesta de observación de un candidato del pre-filtro.

    Solo se admite un símbolo `CANDIDATO` con puntaje: cualquier otro caso es un
    error de quien llama (nunca se inventa un puntaje).
    """
    if datos.estado != EstadoPrefiltro.CANDIDATO:
        raise ValueError(f"{datos.symbol}: solo un CANDIDATO del pre-filtro puede observarse (estado {datos.estado.value})")
    if datos.score_final is None:
        raise ValueError(f"{datos.symbol}: un candidato sin puntaje no puede observarse")

    snapshot = {
        "origen": ORIGEN_SNAPSHOT_OBSERVACION,
        "sin_ia": True,
        "ciclo_id": str(ciclo_id),
        "symbol": datos.symbol,
        "prefiltro": {
            "estado": datos.estado.value,
            "ranking": datos.ranking,
            "score_final": _texto(datos.score_final),
            "score_tendencia": _texto(datos.score_tendencia),
            "score_volumen": _texto(datos.score_volumen),
            "score_distancia": _texto(datos.score_distancia),
            "tendencia_pct": _texto(datos.tendencia_pct),
            "volumen_relativo": _texto(datos.volumen_relativo),
            "distancia_max_90d_pct": _texto(datos.distancia_max_90d_pct),
            "formula_version": datos.formula_version,
            "vela_diaria_base": None if datos.calculated_at is None else datos.calculated_at.isoformat(),
        },
    }
    return PropuestaDeObservacion(
        id=observation_proposal_id(ciclo_id, datos.symbol),
        ciclo_id=ciclo_id,
        symbol=datos.symbol,
        puntaje=datos.score_final.quantize(_DOS_DECIMALES, rounding=ROUND_HALF_EVEN),
        tesis=TESIS_OBSERVACION,
        invalidacion=INVALIDACION_OBSERVACION,
        decision_snapshot=snapshot,
    )
