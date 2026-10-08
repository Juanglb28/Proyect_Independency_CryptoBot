"""Reglas puras sobre el CIERRE de una vela (decisión del usuario, 2026-10-08).

Una vela que todavía se está formando (la última que devuelve Binance en cada
temporalidad) cambia con cada operación del mercado. Si se guarda y se
analiza como si fuera definitiva, el valor de los indicadores depende de la
hora a la que corre el ciclo y la base conserva para siempre un cierre y un
volumen a medias. Decisión: la base guarda la vela en formación marcada como
NO cerrada y la actualiza hasta que cierra; el análisis solo usa velas
CERRADAS.

Todo aquí es puro: sin base de datos, sin red y sin reloj propio (el instante
se recibe como argumento, así el backtest y los tests son reproducibles).
"""

from __future__ import annotations

from datetime import datetime, timedelta

DURACION_POR_TEMPORALIDAD: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
}
"""Duración de cada vela (sección 6.1 del plan: 1h, 4h, 12h y diaria)."""

GRACIA_DE_CIERRE = timedelta(seconds=10)
"""Valor operativo (derivado, no financiero): margen después de la hora de
cierre antes de dar por definitiva una vela que es la más reciente de lo
recibido. Cubre un reloj local ligeramente adelantado respecto del de Binance:
equivocarse hacia "todavía no cerró" solo cuesta una actualización de más;
hacia "ya cerró" congelaría una vela a medias."""


def _exigir_utc(instante: datetime) -> None:
    if instante.tzinfo is None or instante.utcoffset() != timedelta(0):
        raise ValueError("Los instantes deben llevar zona horaria UTC explícita (sección 15.3 del plan).")


def duracion_de_vela(temporalidad: str) -> timedelta:
    try:
        return DURACION_POR_TEMPORALIDAD[temporalidad]
    except KeyError:
        raise ValueError(
            f"Temporalidad no soportada: {temporalidad!r}. Usar una de {tuple(DURACION_POR_TEMPORALIDAD)}."
        ) from None


def hora_de_cierre(open_time: datetime, temporalidad: str) -> datetime:
    """Instante en que la vela que abrió en `open_time` deja de formarse."""
    _exigir_utc(open_time)
    return open_time + duracion_de_vela(temporalidad)


def esta_cerrada(
    open_time: datetime, temporalidad: str, *, ahora: datetime, hay_vela_posterior: bool = False
) -> bool:
    """¿Es definitiva la vela?

    - Si Binance ya entregó una vela POSTERIOR de la misma serie, la anterior
      está cerrada por construcción (no depende del reloj local).
    - Si no, está cerrada cuando `ahora` superó su hora de cierre más
      `GRACIA_DE_CIERRE`.
    """
    _exigir_utc(ahora)
    if hay_vela_posterior:
        return True
    return ahora >= hora_de_cierre(open_time, temporalidad) + GRACIA_DE_CIERRE
