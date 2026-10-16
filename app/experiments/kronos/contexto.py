"""Construcción del contexto que se le entrega a Kronos, con guardia contra look-ahead.

Pregunta que este módulo debe poder responder (master prompt, sección 15):
¿qué sabía exactamente el sistema en `as_of`? Respuesta: solo velas de 1 h
CERRADAS cuyo cierre ocurrió en o antes de `as_of`. Una vela que termina después
de `as_of` se rechaza aunque esté guardada.

Si falta una vela o hay un hueco, NO se rellena ni se interpola (P3): se
devuelve el motivo y el pronóstico no se hace.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from app.data.exchange_client import Candle

UNA_HORA = timedelta(hours=1)


class MotivoSinContexto(str, Enum):
    VELAS_INSUFICIENTES = "VELAS_INSUFICIENTES"
    HUECO_EN_LAS_VELAS = "HUECO_EN_LAS_VELAS"
    ULTIMA_VELA_NO_ES_LA_ESPERADA = "ULTIMA_VELA_NO_ES_LA_ESPERADA"
    VELA_NO_CERRADA = "VELA_NO_CERRADA"
    PRECIO_NO_POSITIVO = "PRECIO_NO_POSITIVO"


@dataclass(frozen=True)
class ContextoKronos:
    symbol: str
    as_of: datetime
    velas: tuple[Candle, ...]  # exactamente `lookback` velas, contiguas, de la más antigua a la más reciente
    horizon: int

    @property
    def ultimo_cierre(self):
        return self.velas[-1]["close"]

    @property
    def aperturas_a_pronosticar(self) -> list[datetime]:
        """`open_time` de las `horizon` velas futuras: as_of, as_of+1h, ..."""
        return [self.as_of + UNA_HORA * k for k in range(self.horizon)]

    @property
    def apertura_de_la_vela_final(self) -> datetime:
        """La vela cuyo cierre es el precio en `as_of + horizon` horas."""
        return self.as_of + UNA_HORA * (self.horizon - 1)


def construir_contexto(
    symbol: str, velas: Sequence[Candle], *, as_of: datetime, lookback: int, horizon: int
) -> ContextoKronos | MotivoSinContexto:
    """Toma las `lookback` velas de 1 h que terminan justo en `as_of`.

    `velas` puede contener más de lo necesario (incluso velas POSTERIORES a
    `as_of`, como en un backtest con todo el historial cargado): solo se usan las
    que existían en `as_of`, y se verifica que sean exactamente las esperadas."""
    if as_of.tzinfo is None or as_of.utcoffset() != timedelta(0):
        raise ValueError("as_of debe tener zona horaria UTC explícita (sección 15.3 del plan).")
    if as_of != as_of.replace(minute=0, second=0, microsecond=0):
        raise ValueError("as_of debe caer en una hora en punto (frontera de vela de 1 h).")
    if lookback <= 0 or horizon <= 0:
        raise ValueError("lookback y horizon deben ser positivos.")

    disponibles = [v for v in velas if v["open_time"] + UNA_HORA <= as_of]  # cerraron en o antes de as_of
    if any(not v["cerrada"] for v in disponibles):
        return MotivoSinContexto.VELA_NO_CERRADA
    disponibles.sort(key=lambda v: v["open_time"])
    if len(disponibles) < lookback:
        return MotivoSinContexto.VELAS_INSUFICIENTES
    ventana = disponibles[-lookback:]
    if ventana[-1]["open_time"] != as_of - UNA_HORA:
        return MotivoSinContexto.ULTIMA_VELA_NO_ES_LA_ESPERADA
    for anterior, actual in zip(ventana, ventana[1:]):
        if actual["open_time"] - anterior["open_time"] != UNA_HORA:
            return MotivoSinContexto.HUECO_EN_LAS_VELAS
    if any(min(v["open"], v["high"], v["low"], v["close"]) <= 0 for v in ventana):
        return MotivoSinContexto.PRECIO_NO_POSITIVO
    return ContextoKronos(symbol=symbol, as_of=as_of.astimezone(timezone.utc), velas=tuple(ventana), horizon=horizon)
