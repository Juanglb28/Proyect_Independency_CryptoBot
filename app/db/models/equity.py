"""Modelo ORM de `equity_observaciones` (Paso 8, decisión del usuario del
2026-10-05): una fila por chequeo del monitor con el equity del sistema y el
pico de equity vigente en ese momento.

Por qué existe: la pausa por drawdown (-15%) se mide contra el PICO de
equity, que incluye el P&L no realizado (decisión del 2026-10-04). Ese pico
sube y baja de forma continua entre chequeos y, sin esta tabla, no hay dónde
recordarlo: `eventos_sistema` solo guarda el pico al pausar o reanudar. P5
exige que el estado sobreviva a un reinicio, y sin el pico guardado un
reinicio lo "olvidaría" y podría ocultar un drawdown real. De paso, esta
tabla es la curva de equity que el Paso 9 reutiliza para el drawdown máximo
y el benchmark.

Reglas:

- El pico vigente es el `peak_equity_usd` de la ÚLTIMA fila (por `id`, no por
  reloj). Sin filas, el pico inicial es el capital paper.
- El pico solo sube dentro de un tramo; tras una reanudación manual se
  reinicia al equity de ese momento (la reanudación escribe su propia fila
  con `origen = REANUDACION`, `peak_equity_usd = equity_usd`).
- Solo-agregar: un trigger rechaza `UPDATE` y `DELETE` (la curva histórica no
  se reescribe — mismo espíritu que la sección 14 del plan).
- `peak_equity_usd >= equity_usd` siempre (el pico es un máximo).

Solo se escribe una fila cuando el equity se pudo calcular COMPLETO (con
precio para todas las posiciones abiertas): nunca se inventa un precio para
rellenar un hueco (P3).

Dinero en `NUMERIC`, fechas en `TIMESTAMPTZ` (UTC) — nunca `float`
(sección 15.3 del plan). Esta tabla no decide cuándo pausar: eso es de
`app/domain/rules/risk.py`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Enum, Identity, Integer, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.enums import OrigenObservacionEquity


class EquityObservacion(Base):
    __tablename__ = "equity_observaciones"
    __table_args__ = (
        CheckConstraint("equity_usd >= 0", name="equity_no_negativo"),
        CheckConstraint("peak_equity_usd > 0", name="pico_positivo"),
        CheckConstraint("peak_equity_usd >= equity_usd", name="pico_no_menor_que_equity"),
        CheckConstraint("open_positions >= 0", name="posiciones_no_negativas"),
        CheckConstraint(
            "origen <> 'REANUDACION' OR peak_equity_usd = equity_usd",
            name="reanudacion_reinicia_pico",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    origen: Mapped[OrigenObservacionEquity] = mapped_column(
        Enum(
            OrigenObservacionEquity,
            name="origen_observacion_equity",
            native_enum=False,
            length=16,
            validate_strings=True,
        ),
        nullable=False,
    )
    equity_usd: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    """Capital + P&L realizado neto + P&L no realizado al precio observado."""
    peak_equity_usd: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    """Pico vigente DESPUÉS de esta observación."""
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False)
    """Cuántas posiciones abiertas se valoraron (para auditar la fila)."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
