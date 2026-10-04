"""Segunda ronda para entradas "de mayor riesgo" (decisión del usuario,
2026-10-04).

## Por qué existe

El 2026-10-03 el ciclo no produjo ninguna propuesta sobre el umbral de
entrada (55) aunque el usuario, mirando LTC, consideraba defendible una
entrada de mayor riesgo (momentum fuerte, RSI alto también contra BTC). En
vez de bajar el umbral global o cambiar los pesos 30/20/20/30 — lo que
contaminaría la validación de la v1 (el objetivo es medir si el score
predice, no maximizar entradas) — se agrega un tier SEPARADO y ETIQUETADO
(`TierEntrada.MAYOR_RIESGO`): el umbral 55 y los pesos quedan intactos, y
al final se puede comparar el rendimiento de este tier contra el normal.

## Qué hace (todo en código — P1; ningún LLM interviene)

Solo se evalúa un candidato cuyo `puntaje_ponderado` esté en
`[SCORE_MINIMO_SEGUNDA_RONDA, SCORE_MINIMO_ENTRADA)` = `[50, 55)`.
Para calificar debe cumplir TODAS estas verificaciones:

1. RSI 14 diario del ratio SÍMBOLO/BTCUSDT > `RSI_RATIO_BTC_MINIMO` (50):
   fuerza relativa frente a BTC.
2. RSI 14 > `RSI_UMBRAL_MOMENTUM` (50) en al menos
   `MIN_TEMPORALIDADES_CON_MOMENTUM` (3) de las 4 temporalidades.
3. RSI 14 diario <= `RSI_DIARIO_TOPE_SOBRECOMPRA` (70): no se entra a un
   activo ya en sobrecompra.

Un dato faltante (RSI sin calcular, ratio sin historia suficiente) NUNCA se
supone ni se rellena (P3): la verificación que dependa de él falla y el
dato queda listado en `faltantes`. En particular BTCUSDT nunca puede
calificar por esta vía — su ratio contra sí mismo es constante y no
define una fuerza relativa; se declara como faltante en vez de saltarse el
criterio con una regla inventada.

## Qué NO hace (a propósito)

No define tamaño ni aprobación: la decisión del usuario es que toda
entrada de este tier requiera APROBACIÓN MANUAL SIEMPRE y use el mínimo
existente de 100 USD (sección 19). Eso se aplica cuando exista el motor de
ejecución (Paso 8, `app/domain/rules/risk.py`); no se introduce hoy ningún
parámetro de dinero nuevo.

    TODO — DECISIÓN PENDIENTE (Paso 8): hacer cumplir "aprobación manual
    siempre + 100 USD" para `TierEntrada.MAYOR_RIESGO` en la validación de
    riesgo.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.enums import TierEntrada
from app.domain.rules.scoring import SCORE_MINIMO_ENTRADA

SCORE_MINIMO_SEGUNDA_RONDA = Decimal(50)
"""Límite inferior (inclusivo) de la zona cercana. Decisión del usuario,
2026-10-04. Nota: ese día LTC quedó en 49.80, es decir, FUERA de la zona —
el usuario mantuvo el corte en 50 a propósito para no ajustarlo mirando un
solo caso."""

RSI_UMBRAL_MOMENTUM = Decimal(50)
MIN_TEMPORALIDADES_CON_MOMENTUM = 3
RSI_DIARIO_TOPE_SOBRECOMPRA = Decimal(70)
RSI_RATIO_BTC_MINIMO = Decimal(50)

TEMPORALIDADES = ("1h", "4h", "12h", "1d")
_TEMPORALIDAD_DIARIA = "1d"


@dataclass(frozen=True)
class SegundaRondaResultado:
    """Resultado auditable de la segunda ronda: además del veredicto se
    guarda el valor de cada dato y de cada verificación, para poder
    reconstruir después exactamente por qué se aprobó o no (sección 15 del
    plan: "¿qué sabía el sistema en ese instante?")."""

    elegible: bool
    criterios: dict
    faltantes: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"elegible": self.elegible, "criterios": self.criterios, "faltantes": list(self.faltantes)}


def en_zona_segunda_ronda(puntaje_ponderado: Decimal) -> bool:
    """`True` si el puntaje está en `[50, 55)`."""
    return SCORE_MINIMO_SEGUNDA_RONDA <= Decimal(puntaje_ponderado) < Decimal(SCORE_MINIMO_ENTRADA)


def evaluar_segunda_ronda(
    *,
    rsi_por_temporalidad: dict[str, Decimal | None],
    rsi_ratio_btc_diario: Decimal | None,
) -> SegundaRondaResultado:
    """Aplica las 3 verificaciones. Los RSI vienen ya calculados (de
    `indicadores` y de `app/data/relative_strength.py`); aquí solo se
    comparan contra los umbrales fijos de este módulo."""
    faltantes: list[str] = []

    # 1. Fuerza relativa vs BTC
    if rsi_ratio_btc_diario is None:
        faltantes.append("rsi_ratio_btc_1d")
        fuerza_relativa_ok = False
    else:
        fuerza_relativa_ok = Decimal(rsi_ratio_btc_diario) > RSI_RATIO_BTC_MINIMO

    # 2. Momentum multitemporal
    con_momentum: list[str] = []
    for tf in TEMPORALIDADES:
        valor = rsi_por_temporalidad.get(tf)
        if valor is None:
            faltantes.append(f"rsi_14_{tf}")
        elif Decimal(valor) > RSI_UMBRAL_MOMENTUM:
            con_momentum.append(tf)
    momentum_ok = len(con_momentum) >= MIN_TEMPORALIDADES_CON_MOMENTUM

    # 3. Tope de sobrecompra (sin RSI diario no se puede verificar -> falla)
    rsi_diario = rsi_por_temporalidad.get(_TEMPORALIDAD_DIARIA)
    sobrecompra_ok = rsi_diario is not None and Decimal(rsi_diario) <= RSI_DIARIO_TOPE_SOBRECOMPRA

    criterios = {
        "fuerza_relativa_btc": {
            "rsi_ratio_1d": None if rsi_ratio_btc_diario is None else str(rsi_ratio_btc_diario),
            "minimo": str(RSI_RATIO_BTC_MINIMO),
            "cumple": fuerza_relativa_ok,
        },
        "momentum_multitemporal": {
            "temporalidades_con_rsi_sobre_umbral": con_momentum,
            "umbral_rsi": str(RSI_UMBRAL_MOMENTUM),
            "minimo_temporalidades": MIN_TEMPORALIDADES_CON_MOMENTUM,
            "cumple": momentum_ok,
        },
        "tope_sobrecompra": {
            "rsi_diario": None if rsi_diario is None else str(rsi_diario),
            "tope": str(RSI_DIARIO_TOPE_SOBRECOMPRA),
            "cumple": sobrecompra_ok,
        },
    }
    return SegundaRondaResultado(
        elegible=fuerza_relativa_ok and momentum_ok and sobrecompra_ok,
        criterios=criterios,
        faltantes=tuple(faltantes),
    )


def clasificar_tier(
    *, puntaje_ponderado: Decimal, supera_umbral_entrada: bool, segunda_ronda: SegundaRondaResultado | None
) -> TierEntrada:
    """`NORMAL` si supera el umbral; `MAYOR_RIESGO` si no, pero cae en la
    zona y la segunda ronda resultó elegible; `NINGUNO` en cualquier otro
    caso (incluida una segunda ronda que nunca se evaluó)."""
    if supera_umbral_entrada:
        return TierEntrada.NORMAL
    if en_zona_segunda_ronda(puntaje_ponderado) and segunda_ronda is not None and segunda_ronda.elegible:
        return TierEntrada.MAYOR_RIESGO
    return TierEntrada.NINGUNO
