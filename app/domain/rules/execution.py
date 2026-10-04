"""Selección de las propuestas que se convierten en órdenes (sección 9 del
plan y "Decisiones del Paso 8" del README, cerradas el 2026-10-04).

Función PURA: recibe las propuestas candidatas y la foto de los cupos, y
devuelve cuáles se abren y cuáles se descartan (con todos sus motivos). No
toca la base de datos ni crea órdenes — eso lo hace el servicio de
`app/execution/` con lo que esta función decida. Los agentes de IA nunca
llegan aquí: el `tier` y el puntaje ya fueron calculados en código (P1/P2).

Orden de prioridad (decisión del usuario, 2026-10-04):

1. las `NORMAL` entran antes que las `MAYOR_RIESGO`, aunque tengan menor
   puntaje;
2. dentro de cada tier, de mayor a menor `puntaje_ponderado`;
3. ante un empate exacto, por símbolo en orden alfabético y luego por id de
   propuesta. El plan no define el desempate; se necesita UNO para que el
   resultado sea reproducible (misma entrada, misma salida, en el ciclo y
   en el backtest) — es solo un desempate de determinismo, no una
   preferencia por ningún activo, y es fácil de cambiar si se decide otro.

Reserva de cupos (decisión del usuario, 2026-10-04): una entrada que queda
esperando aprobación manual OCUPA cupo (de las 4 posiciones, las 3 entradas
del día y, si es de mayor riesgo, del tope de 1) hasta que se apruebe, se
rechace o expire. Por eso aquí toda entrada seleccionada consume cupo para
las siguientes, sin importar si se ejecutará sola o pedirá aprobación. El
servicio es quien debe armar `SituacionDeCupos` contando también las
órdenes `PENDING_APPROVAL` — ver el docstring de esa clase.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from decimal import Decimal

from app.domain.enums import TierEntrada
from app.domain.rules.risk import (
    TAMANO_POSICION_USD,
    MotivoRechazoEntrada,
    SituacionDeCupos,
    validate_entry,
)

_PRIORIDAD_TIER = {TierEntrada.NORMAL: 0, TierEntrada.MAYOR_RIESGO: 1, TierEntrada.NINGUNO: 2}


@dataclass(frozen=True)
class CandidatoEntrada:
    """Una propuesta ya clasificada, lista para competir por un cupo."""

    proposal_id: uuid.UUID
    symbol: str
    tier: TierEntrada
    puntaje_ponderado: Decimal


@dataclass(frozen=True)
class EntradaSeleccionada:
    candidato: CandidatoEntrada
    requiere_aprobacion_manual: bool
    monto_usd: Decimal


@dataclass(frozen=True)
class EntradaDescartada:
    candidato: CandidatoEntrada
    motivos: tuple[MotivoRechazoEntrada, ...]


@dataclass(frozen=True)
class ResultadoSeleccion:
    seleccionadas: tuple[EntradaSeleccionada, ...]
    descartadas: tuple[EntradaDescartada, ...]


def _clave_de_orden(c: CandidatoEntrada) -> tuple:
    return (_PRIORIDAD_TIER[c.tier], -c.puntaje_ponderado, c.symbol, str(c.proposal_id))


def select_entries(
    candidatos: list[CandidatoEntrada] | tuple[CandidatoEntrada, ...],
    cupos: SituacionDeCupos,
    *,
    monto_usd: Decimal = TAMANO_POSICION_USD,
) -> ResultadoSeleccion:
    """Recorre los candidatos en orden de prioridad y valida cada uno con
    `risk.validate_entry` contra los cupos que quedan. Cada entrada aceptada
    consume cupo para las siguientes (una posición, una entrada del día, el
    símbolo y, si es de mayor riesgo, su tope propio). Las rechazadas no
    consumen nada. Devuelve ambos grupos, cada uno en orden de prioridad."""
    seleccionadas: list[EntradaSeleccionada] = []
    descartadas: list[EntradaDescartada] = []
    cupos_actuales = cupos

    for candidato in sorted(candidatos, key=_clave_de_orden):
        validacion = validate_entry(
            symbol=candidato.symbol, tier=candidato.tier, cupos=cupos_actuales, monto_usd=monto_usd
        )
        if not validacion.permitida:
            descartadas.append(EntradaDescartada(candidato=candidato, motivos=validacion.motivos_rechazo))
            continue

        seleccionadas.append(
            EntradaSeleccionada(
                candidato=candidato,
                requiere_aprobacion_manual=validacion.requiere_aprobacion_manual,
                monto_usd=validacion.monto_usd,
            )
        )
        cupos_actuales = replace(
            cupos_actuales,
            posiciones_abiertas=cupos_actuales.posiciones_abiertas + 1,
            entradas_hoy=cupos_actuales.entradas_hoy + 1,
            mayor_riesgo_abiertas=cupos_actuales.mayor_riesgo_abiertas
            + (1 if candidato.tier == TierEntrada.MAYOR_RIESGO else 0),
            simbolos_abiertos=cupos_actuales.simbolos_abiertos | {candidato.symbol},
        )

    return ResultadoSeleccion(seleccionadas=tuple(seleccionadas), descartadas=tuple(descartadas))
