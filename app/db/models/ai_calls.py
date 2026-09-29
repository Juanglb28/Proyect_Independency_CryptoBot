"""Modelo ORM de `llamadas_ia` (sección 6 del plan, Paso 6): "Registro de
cada llamada: agente, modelo, tokens, costo, respuesta". No es opcional —
sin ella no se puede auditar por qué el sistema interpretó algo de cierta
forma ni calcular el costo real de operar (sección 6, sección 4.3).

`ciclo_id` es nullable a propósito: hoy no existe ningún orquestador que
cree filas en `ciclos_analisis` (`app/orchestration/daily_cycle.py` no
está implementado — Paso 7 en adelante), así que exigir la FK ahora
impediría probar los agentes de forma aislada (por ejemplo desde
`scripts/run_agent_demo.py`). Cuando el orquestador exista, cada llamada
real de un ciclo debería llevar su `ciclo_id`.

`prompt_version` guarda un identificador corto del prompt usado (hoy
siempre `"v1"`, la única versión que existe — sección 7.2 del plan: "Los
prompts siguientes son la versión inicial. Se refinan con uso real."). Es
necesario para la trazabilidad del `decision_snapshot` (sección 14 del
plan: "versión del prompt" es uno de los campos que se congela junto a
cada decisión de entrada).

`respuesta` guarda el JSON YA VALIDADO contra el schema Pydantic del
agente (nunca el texto crudo del modelo): si el modelo devolviera texto
fuera de formato, la llamada falla antes de llegar aquí (ver
`app/ai/client.py` y `app/ai/agents/*.py`) — esta tabla es el registro de
llamadas que sí produjeron una interpretación válida.

`cost_usd` se calcula en código a partir de tokens y una tabla de precios
fija en `app/ai/models.py` (verificada contra la documentación de
Anthropic, no inventada) — es una cifra de observabilidad interna para
seguir el gasto proyectado (sección 4.3/11 del plan: "~8 USD/mes"), no la
factura real, que siempre es la que reporta la consola de Anthropic.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LlamadaIA(Base):
    __tablename__ = "llamadas_ia"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    ciclo_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ciclos_analisis.id"), nullable=True
    )

    agente: Mapped[str] = mapped_column(nullable=False)
    modelo: Mapped[str] = mapped_column(nullable=False)
    prompt_version: Mapped[str] = mapped_column(nullable=False)

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)

    respuesta: Mapped[dict] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
