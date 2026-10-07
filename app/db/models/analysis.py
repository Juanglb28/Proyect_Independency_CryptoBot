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

## `PrefiltroResultado`

Una fila por cada símbolo del universo y por cada ciclo: lo que hizo el
pre-filtro determinista ese día (puntaje, sub-puntajes, ranking y si pasó el
corte — `EstadoPrefiltro`), incluidos los descartados y los `SIN_DATOS` (sin
puntaje: los campos numéricos quedan `NULL`, nunca un valor de relleno, P3).
Existe para poder responder después "qué se evaluó y qué se dejó fuera ese
día" (sección 8.9, Decisiones) y, más adelante, medir el propio pre-filtro.

Solo-agregar (un trigger rechaza `UPDATE`/`DELETE`, como `perfil_usuario`):
es evidencia de lo que el sistema sabía en ese instante (sección 14/15 del
plan). Los ciclos anteriores a esta tabla (2026-10-07) NO se rellenan
retroactivamente: no hay forma honesta de saber qué sabía el sistema
entonces. `formula_version` guarda la versión de la fórmula
(`app.data.prefilter.PREFILTER_FORMULA_VERSION`) para interpretar el puntaje
aunque los límites de normalización se recalibren.

Es una tabla adicional a las 13 del master prompt, aprobada por el usuario
(2026-10-07).

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

    DECIDIDO (Paso 8): `initial_stop`/`initial_target` NO se agregan aquí. Se
    calculan al abrir la posición sobre el precio de EJECUCIÓN real y quedan
    inmutables en `posiciones.initial_stop_price`/`initial_target_price`; este
    snapshot no se reescribe.

Una vez creada, ninguna fila de `Propuesta` (ni su `decision_snapshot`) se
modifica — es historia congelada (sección 14: "no modificar
retrospectivamente el snapshot").
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.enums import EstadoCiclo, EstadoPrefiltro, TierEntrada


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
    ciclos_consecutivos_en_zona: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Días de análisis seguidos (contando este) en la zona cercana
    `[50, 55)` — ver `app.domain.rules.entry_tier.contar_ciclos_consecutivos_en_zona`.
    Solo observación: NO afecta `tier_entrada`. 0 si el puntaje de esta
    propuesta no está en la zona; NULL en las filas anteriores a esta
    columna (2026-10-04), que no se rellenan retroactivamente."""
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


class PrefiltroResultado(Base):
    __tablename__ = "prefiltro_resultados"
    __table_args__ = (
        UniqueConstraint("ciclo_id", "symbol", name="uq_prefiltro_resultados_ciclo_id_symbol"),
        CheckConstraint(
            "(estado = 'SIN_DATOS' AND ranking IS NULL AND score_final IS NULL AND calculated_at IS NULL) "
            "OR (estado <> 'SIN_DATOS' AND ranking IS NOT NULL AND score_final IS NOT NULL "
            "AND calculated_at IS NOT NULL)",
            name="sin_datos_coherente",
        ),
        CheckConstraint("ranking IS NULL OR ranking >= 1", name="ranking_positivo"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    ciclo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ciclos_analisis.id"), nullable=False
    )

    symbol: Mapped[str] = mapped_column(nullable=False)
    estado: Mapped[EstadoPrefiltro] = mapped_column(
        Enum(EstadoPrefiltro, name="estado_prefiltro", native_enum=False, length=32, validate_strings=True),
        nullable=False,
    )
    ranking: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """1 = mejor puntaje del día entre los símbolos con puntaje. NULL en `SIN_DATOS`."""

    calculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """`open_time` de la vela diaria sobre la que se calculó. NULL en `SIN_DATOS`."""

    tendencia_pct: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    volumen_relativo: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    distancia_max_90d_pct: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    score_tendencia: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    score_volumen: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    score_distancia: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    score_final: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)

    formula_version: Mapped[str] = mapped_column(nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
