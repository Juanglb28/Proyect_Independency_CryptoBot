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

Un retorno es una fracción del valor de entrada (0,05 = +5 % neto). Si los
datos terminan antes de cerrar toda la posición, devuelve `None` (censurada):
no se cuenta ni a favor ni en contra.

Solo `Decimal`; sin base de datos, red ni LLM.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class SalidaSimulada:
    retorno_neto: Decimal
    """Fracción del valor de entrada, ya con comisiones de entrada y salidas."""
    velas_hasta_cierre: int
    """Velas posteriores a la de entrada hasta el cierre TOTAL de la posición."""
    motivo: str
    """`STOP_LOSS` o `TRAILING_STOP`."""


def simular_salida(velas: list[Candle], indice_entrada: int) -> SalidaSimulada | None:
    """Entra al cierre de `velas[indice_entrada]` y aplica las reglas de salida
    del motor paper sobre las velas siguientes."""
    entrada = velas[indice_entrada]["close"]
    niveles = calculate_initial_levels(entrada)
    cantidad = _UNO / entrada  # valor de entrada normalizado a 1
    comision_entrada = COMISION_POR_LADO

    cobrado = Decimal(0)  # lo recibido por las ventas, ya sin comisión de venta
    restante = cantidad
    pico: Decimal | None = None

    for offset, vela in enumerate(velas[indice_entrada + 1 :], start=1):
        if pico is None:  # todavía sin cierre parcial
            if vela["low"] <= niveles.stop_price:
                ingreso = restante * niveles.stop_price
                cobrado += ingreso * (_UNO - COMISION_POR_LADO)
                return SalidaSimulada(cobrado - _UNO - comision_entrada, offset, "STOP_LOSS")
            if vela["high"] >= niveles.target_price:
                vendida = restante * TAKE_PROFIT_FRACCION_A_CERRAR
                cobrado += vendida * niveles.target_price * (_UNO - COMISION_POR_LADO)
                restante -= vendida
                pico = niveles.target_price
            continue

        pico = max(pico, vela["high"])
        trailing = calculate_trailing_stop(pico)
        if vela["low"] <= trailing:
            cobrado += restante * trailing * (_UNO - COMISION_POR_LADO)
            return SalidaSimulada(cobrado - _UNO - comision_entrada, offset, "TRAILING_STOP")
    return None
