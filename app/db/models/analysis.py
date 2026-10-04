"""Modelos ORM de `ciclos_analisis`, `factores` y `propuestas` (sección 6
del plan). Viven en el mismo archivo porque la sección 7 (estructura del
proyecto) solo declara `db/models/analysis.py` para este grupo — a
diferencia de la capa de ejecución/resultados, que sí tiene un archivo por
entidad (`orders.py`, `positions.py`, `trade_results.py`...), las tres
tablas de este módulo son las tres caras del mismo ciclo de análisis
(Paso 7): el ciclo en sí, sus factores puntuados, y las propuestas que
produjo.

## `CicloAnalisis`

Registra cada ejecución del ciclo diario (sección 15.6). `status` guarda
la ÚLTIMA etapa de `app.domain.enums.EstadoCiclo` alcanzada — nunca un
resultado resumido inventado (ver el docstring de ese enum sobre cómo se
deriva éxito/sin-candidatos/fallo a partir de `status` + `error_message` +
la existencia de `propuestas`).

## `Factor`

Una fila por cada una de las 4 categorías ponderadas que juzga el
Consolidador (`tendencia_momentum`/`volumen_liquidez`/`posicion_rangos_ath`/
`contexto_eventos`) para una `Propuesta` — decisión del usuario
(2026-09-30): es la única lectura de "cada factor puntuado... con peso,
puntaje y confianza" (sección 6 del plan) que no inventa una granularidad
nueva, porque calza exactamente con lo que
`app.domain.rules.scoring.calculate_consolidated_score` ya combina.

`peso` se guarda en la fila (no se relee de `scoring.py` al consultar
historial): si los pesos fijos cambiaran en una versión futura del
proyecto, las filas ya persistidas deben seguir reflejando el peso que
realmente se usó para calcular esa propuesta — mismo principio que "no
modificar retrospectivamente el snapshot" (sección 14 del plan).

`confianza` es `confianza_global` de la opción del Consolidador (no existe
una confianza más fina por categoría en `ConsolidadorOutput` — ver
`app/ai/schemas.py`), repetida en sus 4 filas de factores.

## `Propuesta`

Una fila por cada opción que devolvió el Consolidador, CON su
`ScoreConsolidado` ya calculado en código (`app/domain/rules/scoring.py`,
nunca por el LLM — P1 del plan). Decisión del usuario (2026-09-30): se
persisten TODAS las opciones que el Consolidador devuelve, incluidas las
que no superan `SCORE_MINIMO_ENTRADA` (`supera_umbral_entrada=False`) —
trazabilidad completa; decidir cuál se convierte en orden paper es
responsabilidad de Paso 8, no de este paso.

`decision_snapshot` (JSONB, sección 8.3 del plan): congela el contexto
completo que sustentó la propuesta — precio de referencia, indicadores por
temporalidad, contexto de eventos, los tres scores, tesis/invalidación,
versión de modelo y de prompt de cada agente. Deliberadamente NO incluye
`initial_stop`/`initial_target` en este paso — decisión del usuario
(2026-09-30): son reglas de riesgo (sección 19) y hoy no existe
`app/domain/rules/risk.py`; crearlo sin ninguna posición real que proteger
sería sobreimplementar (sección 29 del master prompt).

    TODO — DECISIÓN PENDIENTE (Paso 8): agregar `initial_stop`/
    `initial_target` al decision_snapshot cuando exista
    `app/domain/rules/risk.py` con las reglas fijas de la sección 19.

Una vez creada, ninguna fila de `Propuesta` (ni su `decision_snapshot`) se
modifica — es historia congelada (sección 14: "no modificar
retrospectivamente el snapshot").
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.enums import EstadoCiclo, TierEntrada


class CicloAnalisis(Base):
    __tablename__ = "ciclos_analisis"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[EstadoCiclo] = mapped_column(
        Enum(EstadoCiclo, name="estado_ciclo", native_enum=False, length=32, validate_strings=True),
        nullable=False,
    )
    """Última etapa de `EstadoCiclo` alcanzada por este ciclo — ver el
    docstring del módulo sobre cómo derivar el resultado real.
    `native_enum=False`: se guarda como `VARCHAR` validado en Python, no un
    tipo ENUM nativo de Postgres — agregar una etapa nueva más adelante
    (Paso 8/9) no debe requerir un `ALTER TYPE` en la base de datos."""

    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Propuesta(Base):
    __tablename__ = "propuestas"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ciclo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ciclos_analisis.id"), nullable=False
    )

    symbol: Mapped[str] = mapped_column(nullable=False)
    categoria: Mapped[str] = mapped_column(nullable=False)
    """"principal" o "menor_riesgo" (`OpcionConsolidada.categoria`)."""
    horizonte: Mapped[str] = mapped_column(nullable=False)
    nivel_riesgo: Mapped[str] = mapped_column(nullable=False)

    puntaje_base: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    calidad_datos: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    puntaje_ponderado: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    supera_umbral_entrada: Mapped[bool] = mapped_column(Boolean, nullable=False)

    tier_entrada: Mapped[TierEntrada | None] = mapped_column(
        Enum(TierEntrada, name="tier_entrada", native_enum=False, length=16, validate_strings=True),
        nullable=True,
    )
    """`NORMAL` / `MAYOR_RIESGO` / `NINGUNO` (ver `app.domain.enums.TierEntrada`
    y `app/domain/rules/entry_tier.py`). NULL en las filas anteriores a esta
    columna (2026-10-04): esas propuestas se crearon antes de que existiera
    el criterio, y rellenarlas retroactivamente sería reescribir historia
    congelada (sección 14 del plan) — se dejan en NULL a propósito, no
    como un valor inventado."""
    segunda_ronda: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    """Evidencia de la segunda ronda (valores y veredicto de cada
    verificación, y datos faltantes). NULL si la propuesta no cayó en la
    zona `[50, 55)` y por lo tanto la segunda ronda nunca se evaluó."""

    tesis: Mapped[str] = mapped_column(Text, nullable=False)
    invalidacion: Mapped[str] = mapped_column(Text, nullable=False)
    escenario_adverso: Mapped[dict] = mapped_column(JSONB, nullable=False)
    comentario_chartista: Mapped[str | None] = mapped_column(Text, nullable=True)
    fuentes: Mapped[list] = mapped_column(JSONB, nullable=False)
    confianza_global: Mapped[str] = mapped_column(nullable=False)

    decision_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Factor(Base):
    __tablename__ = "factores"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ciclo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ciclos_analisis.id"), nullable=False
    )
    propuesta_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("propuestas.id"), nullable=False
    )

    symbol: Mapped[str] = mapped_column(nullable=False)
    factor: Mapped[str] = mapped_column(nullable=False)
    """Una de: "tendencia_momentum", "volumen_liquidez",
    "posicion_rangos_ath", "contexto_eventos" — ver
    `app.domain.rules.scoring`."""
    peso: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    puntaje: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    confianza: Mapped[str | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
