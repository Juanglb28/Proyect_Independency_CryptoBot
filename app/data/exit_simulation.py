"""Simulación SECUNDARIA de la salida real (estudio H1, 2026-10-08).

La tasa "objetivo primero" del estudio de barreras supone una salida completa
en +12 % o -8 %. El motor paper hace otra cosa: cierra el 50 % en +12 % y deja
el resto con un trailing del 6 %, y cobra comisión en cada orden. Este módulo
reproduce ESAS reglas sobre velas de 1h para estimar el retorno neto de una
entrada, con las mismas constantes y la misma función de trailing que
`app/domain/rules/risk.py`. Es una estimación para el estudio, NO una regla ni
un backtest del sistema completo.

Convenciones sobre una vela OHLC (no se sabe el orden intravela; se elige la
lectura pesimista donde importa y se documenta):

- Antes del objetivo, si una vela toca el stop y el objetivo a la vez, cuenta
  como STOP (pesimista; el estudio de barreras la separa como AMBIGUA).
- El stop se ejecuta al nivel del stop y el cierre parcial al nivel del
  objetivo (el motor real ejecuta al precio observado, que puede ser peor en un
  gap; aquí no se modelan gaps ni slippage, igual que el paper: slippage = 0).
- Tras el cierre parcial, el resto de esa vela no se evalúa y el máximo del
  trailing arranca en el nivel del objetivo. En cada vela siguiente el máximo
  sube con el `high` y luego se compara el `low` con el trailing de ese máximo
  (se asume que el máximo precede al mínimo); la salida es al nivel del trailing.
- Comisión 0,1 % por lado sobre cada ejecución (`COMISION_POR_LADO`).

Orden intravela del trailing (parámetro `orden_intravela`): con velas de 1h no se
sabe si el máximo o el mínimo ocurrió primero. El valor por defecto
(`MAXIMO_PRIMERO`) es la lectura de todos los resultados ya publicados y NO cambia.
`MINIMO_PRIMERO` es la lectura INVERSA (el mínimo precede al máximo: se compara el
`low` con el trailing del máximo ANTERIOR y solo después sube el máximo). Ninguna
de las dos es «la pesimista» en general: si el `low` toca el trailing viejo, la
inversa sale a un nivel más bajo; si solo toca el trailing nuevo, la inversa NO
sale en esa vela y la posición sigue (con el máximo ya subido). Por eso se miden
las dos y la realidad cae entre ambas (`scripts/estudio_orden_intravela.py`);
ninguna regla viva ni resultado previo usa la inversa.

Resolución con velas de 1 minuto (parámetro `resolvedor`, estudio 2026-10-17): en las
pocas velas de 1h cuyo desenlace depende del orden intravela (`clase_ambiguedad`), la
simulación puede consultar un `resolvedor` que devuelve las velas de 1 minuto de esa
hora, y recorre ESAS velas con las mismas reglas (el máximo del trailing y el cierre
parcial se actualizan minuto a minuto, incluido el resto de la hora en que se toca el
objetivo). Si el resolvedor no devuelve datos válidos, se usa la regla de 1h de
siempre. Sin resolvedor (el valor por defecto) NADA cambia: es la lectura de todos
los resultados publicados. Dentro de un minuto se sigue usando `orden_intravela`.

Un retorno es una fracción del valor de entrada (0,05 = +5 % neto). Si los
datos terminan antes de cerrar toda la posición, devuelve `None` (censurada):
no se cuenta ni a favor ni en contra.

Solo `Decimal`; sin base de datos, red ni LLM.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from app.data.exchange_client import Candle
from app.domain.rules.risk import (
    COMISION_POR_LADO,
    TAKE_PROFIT_FRACCION_A_CERRAR,
    calculate_initial_levels,
    calculate_trailing_stop,
)

_UNO = Decimal(1)

MAXIMO_PRIMERO = "maximo_primero"
"""Lectura por defecto: en la vela, el máximo precede al mínimo."""
MINIMO_PRIMERO = "minimo_primero"
"""Lectura inversa: en la vela, el mínimo precede al máximo."""
_ORDENES_INTRAVELA = (MAXIMO_PRIMERO, MINIMO_PRIMERO)


AMBIGUA_STOP_Y_OBJETIVO = "stop_y_objetivo"
"""Antes del cierre parcial, la vela toca el stop Y el objetivo (la regla de 1h cuenta STOP)."""
AMBIGUA_PARCIAL_Y_TRAILING = "parcial_y_trailing"
"""La vela toca el objetivo y su rango bastaría para tocar también el trailing (la regla
de 1h no evalúa el resto de esa vela)."""
AMBIGUA_ORDEN_TRAILING = "orden_trailing"
"""Tras el cierre parcial: el máximo sube y el mínimo toca el trailing nuevo; las dos
lecturas de `orden_intravela` dan un desenlace distinto."""

ResolvedorIntravela = Callable[[Candle, str], "list[Candle] | None"]
"""`(vela_1h, clase) -> velas de 1 minuto de esa hora`, o `None` si no hay datos válidos."""


def clase_ambiguedad(vela: Candle, *, niveles, pico: Decimal | None) -> str | None:
    """Clase de ambigüedad intravela de `vela` dado el estado de la posición, o `None`
    si TODAS las lecturas posibles dan el mismo desenlace (no hace falta mirar minutos).

    `pico` es `None` antes del cierre parcial. Es una condición necesaria y suficiente
    para que el orden dentro de la hora importe, no una aproximación."""
    if pico is None:
        if vela["high"] >= niveles.target_price and vela["low"] <= calculate_trailing_stop(vela["high"]):
            return AMBIGUA_STOP_Y_OBJETIVO if vela["low"] <= niveles.stop_price else AMBIGUA_PARCIAL_Y_TRAILING
        return None
    if vela["high"] > pico and vela["low"] <= calculate_trailing_stop(vela["high"]):
        return AMBIGUA_ORDEN_TRAILING
    return None


def _validar_orden(orden_intravela: str) -> None:
    if orden_intravela not in _ORDENES_INTRAVELA:
        raise ValueError(f"orden_intravela debe ser uno de {_ORDENES_INTRAVELA} (recibido {orden_intravela!r}).")


@dataclass(frozen=True)
class SalidaSimulada:
    retorno_neto: Decimal
    """Fracción del valor de entrada, ya con comisiones de entrada y salidas."""
    velas_hasta_cierre: int
    """Velas posteriores a la de entrada hasta el cierre TOTAL de la posición."""
    motivo: str
    """`STOP_LOSS` o `TRAILING_STOP`."""


@dataclass(frozen=True)
class _Recorrido:
    salida: SalidaSimulada | None
    cobrado: Decimal
    restante: Decimal
    comision_entrada: Decimal


class _Estado:
    """Posición simulada: lo cobrado, lo que queda y el pico del trailing (`None`
    mientras no hubo cierre parcial)."""

    __slots__ = ("niveles", "cobrado", "restante", "pico")

    def __init__(self, niveles, cantidad: Decimal) -> None:
        self.niveles = niveles
        self.cobrado = Decimal(0)  # lo recibido por las ventas, ya sin comisión de venta
        self.restante = cantidad
        self.pico: Decimal | None = None


def _paso(e: _Estado, vela: Candle, orden_intravela: str) -> str | None:
    """Aplica UNA vela (de 1h o de 1 minuto) a la posición. Devuelve el motivo de
    cierre total (`STOP_LOSS` / `TRAILING_STOP`) o `None` si la posición sigue."""
    niveles = e.niveles
    if e.pico is None:  # todavía sin cierre parcial
        if vela["low"] <= niveles.stop_price:
            e.cobrado += e.restante * niveles.stop_price * (_UNO - COMISION_POR_LADO)
            e.restante = Decimal(0)
            return "STOP_LOSS"
        if vela["high"] >= niveles.target_price:
            vendida = e.restante * TAKE_PROFIT_FRACCION_A_CERRAR
            e.cobrado += vendida * niveles.target_price * (_UNO - COMISION_POR_LADO)
            e.restante -= vendida
            e.pico = niveles.target_price
        return None

    if orden_intravela == MINIMO_PRIMERO:
        trailing_previo = calculate_trailing_stop(e.pico)
        if vela["low"] <= trailing_previo:
            e.cobrado += e.restante * trailing_previo * (_UNO - COMISION_POR_LADO)
            e.restante = Decimal(0)
            return "TRAILING_STOP"
        e.pico = max(e.pico, vela["high"])
        return None

    e.pico = max(e.pico, vela["high"])
    trailing = calculate_trailing_stop(e.pico)
    if vela["low"] <= trailing:
        e.cobrado += e.restante * trailing * (_UNO - COMISION_POR_LADO)
        e.restante = Decimal(0)
        return "TRAILING_STOP"
    return None


def _recorrer(
    velas: list[Candle],
    indice_entrada: int,
    orden_intravela: str = MAXIMO_PRIMERO,
    resolvedor: ResolvedorIntravela | None = None,
) -> _Recorrido:
    """Camino ÚNICO de la simulación: lo usan `simular_salida` y
    `retorno_marcado_a_mercado`, así no hay dos copias de las reglas."""
    _validar_orden(orden_intravela)
    entrada = velas[indice_entrada]["close"]
    estado = _Estado(calculate_initial_levels(entrada), _UNO / entrada)  # valor de entrada normalizado a 1
    comision_entrada = COMISION_POR_LADO

    for offset, vela in enumerate(velas[indice_entrada + 1 :], start=1):
        motivo: str | None = None
        minutos = None
        if resolvedor is not None:
            clase = clase_ambiguedad(vela, niveles=estado.niveles, pico=estado.pico)
            if clase is not None:
                minutos = resolvedor(vela, clase)
        if minutos:
            for minuto in minutos:
                motivo = _paso(estado, minuto, orden_intravela)
                if motivo is not None:
                    break
        else:
            motivo = _paso(estado, vela, orden_intravela)
        if motivo is not None:
            return _Recorrido(
                SalidaSimulada(estado.cobrado - _UNO - comision_entrada, offset, motivo),
                estado.cobrado, Decimal(0), comision_entrada,
            )
    return _Recorrido(None, estado.cobrado, estado.restante, comision_entrada)


def simular_salida(
    velas: list[Candle],
    indice_entrada: int,
    *,
    orden_intravela: str = MAXIMO_PRIMERO,
    resolvedor: ResolvedorIntravela | None = None,
) -> SalidaSimulada | None:
    """Entra al cierre de `velas[indice_entrada]` y aplica las reglas de salida
    del motor paper sobre las velas siguientes."""
    return _recorrer(velas, indice_entrada, orden_intravela, resolvedor).salida


def retorno_marcado_a_mercado(
    velas: list[Candle],
    indice_entrada: int,
    *,
    orden_intravela: str = MAXIMO_PRIMERO,
    resolvedor: ResolvedorIntravela | None = None,
) -> Decimal | None:
    """Retorno neto si la posición se VALORARA al cierre de la última vela
    disponible (lo ya cobrado + lo que queda, con la comisión de una venta).

    Para una posición ya cerrada devuelve el mismo retorno que `simular_salida`.
    Sirve SOLO para el diagnóstico de sesgo de censura del backtest: una
    posición abierta al terminar los datos no tiene resultado definitivo y este
    valor es una estimación a precio de mercado, no un resultado. `None` si no
    hay ninguna vela posterior a la de entrada."""
    if indice_entrada + 1 >= len(velas):
        return None
    r = _recorrer(velas, indice_entrada, orden_intravela, resolvedor)
    if r.salida is not None:
        return r.salida.retorno_neto
    ultimo = velas[-1]["close"]
    return r.cobrado + r.restante * ultimo * (_UNO - COMISION_POR_LADO) - _UNO - r.comision_entrada
