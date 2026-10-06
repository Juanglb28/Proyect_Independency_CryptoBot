"""Schemas Pydantic de la salida de cada agente (sección 7.2 del plan,
Paso 6 — "Schemas Pydantic, cliente Anthropic, agentes y registro de
llamadas_ia").

Cada schema reproduce EXACTAMENTE el bloque "SALIDA" del prompt
correspondiente en el plan — mismos nombres de campo (en español, tal como
están escritos ahí), mismos valores permitidos en los campos enumerados.
No se renombra nada a inglés ni se "mejora" la forma del JSON: cambiar esto
aquí sin cambiar el prompt real haría que la validación rechazara
respuestas correctas del modelo.

El Agente 5 (Consolidador) sí tiene schema (`ConsolidadorOutput`, más
abajo), diseñado junto con el usuario el 2026-09-29 porque la sección 7.2
del plan describe su prompt (prohibiciones, esquema de ponderación, "tres
opciones... y dos opciones adicionales") pero nunca llega a mostrar un
bloque JSON de "SALIDA" explícito como sí tienen los Agentes 2/3/4.

Decisión clave de ese diseño (P1 del plan: "el código calcula, la IA
interpreta"): el agente NUNCA calcula `puntaje_base` ni `puntaje_ponderado`
— solo entrega su juicio cualitativo de cada categoría como un número
0-100 (`OpcionConsolidada`). La combinación con los pesos fijos y el
multiplicador de calidad de datos la hace
`app.domain.rules.scoring.calculate_consolidated_score`, código
determinista y testeado — ver ese módulo para el porqué (incluye una
contradicción real detectada en la sección 7.2 del plan entre su tabla de
ponderación y su descripción textual, resuelta con el usuario).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _AgentOutputBase(BaseModel):
    """Base común: rechaza campos no esperados en vez de ignorarlos en
    silencio, para notar de inmediato si el modelo empieza a devolver una
    forma distinta a la pedida (deriva de prompt)."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Agente 2 — Analista cuantitativo
# ---------------------------------------------------------------------------


class Observacion(_AgentOutputBase):
    factor: str
    lectura: str
    direccion: Literal["favorable", "desfavorable", "neutro"]
    fuerza: int
    campos_usados: list[str]


class AnalistaCuantitativoOutput(_AgentOutputBase):
    simbolo: str
    observaciones: list[Observacion]
    datos_faltantes: list[str]
    conflictos_entre_temporalidades: str
    confianza: Literal["alta", "media", "baja"]
    justificacion_confianza: str


# ---------------------------------------------------------------------------
# Agente 3 — Analista de eventos
# ---------------------------------------------------------------------------


class EventoComparable(_AgentOutputBase):
    evento_id: str
    semejanza: Literal["alta", "media", "baja"]
    razon_semejanza: str
    comportamiento_posterior: str


class AnalistaEventosOutput(_AgentOutputBase):
    simbolo: str
    eventos_comparables: list[EventoComparable]
    tamano_muestra: int
    consistencia_historica: Literal["consistente", "mixta", "contradictoria"]
    lectura_general: str
    confianza: Literal["alta", "media", "baja"]
    anomalias_detectadas: list[str]


# ---------------------------------------------------------------------------
# Agente 4 — Auditor de datos
# ---------------------------------------------------------------------------


class AuditorOutput(_AgentOutputBase):
    campos_faltantes: list[str]
    huecos_en_series: list[str]
    valores_anomalos: list[str]
    antiguedad_maxima_horas: float
    calidad_global: Literal["suficiente", "limitada", "insuficiente"]
    penalizacion_confianza_pct: float
    bloqueantes: list[str]


# ---------------------------------------------------------------------------
# Agente 5 — Consolidador
# ---------------------------------------------------------------------------


class EscenarioAdversoConsolidado(_AgentOutputBase):
    descripcion: str
    desencadenante: str
    probabilidad_relativa: Literal["baja", "media", "alta"]


class OpcionConsolidada(_AgentOutputBase):
    """Una opción del Consolidador. Los cuatro campos `*_momentum`/
    `*_liquidez`/`*_rangos_ath`/`*_eventos` son el JUICIO del agente sobre
    cada categoría (0-100) — no un puntaje ya ponderado. `puntaje_base` y
    `puntaje_ponderado` NO existen en este schema a propósito: los calcula
    `app.domain.rules.scoring.calculate_consolidated_score` después de
    validar esta respuesta, nunca el agente."""

    simbolo: str
    categoria: Literal["principal", "menor_riesgo"]
    horizonte: Literal["corto", "medio", "largo"]
    nivel_riesgo: Literal["bajo", "medio", "alto"]

    tendencia_momentum: float = Field(ge=0, le=100)
    volumen_liquidez: float = Field(ge=0, le=100)
    posicion_rangos_ath: float = Field(ge=0, le=100)
    contexto_eventos: float = Field(ge=0, le=100)

    tesis: str
    invalidacion: str
    escenario_adverso: EscenarioAdversoConsolidado
    comentario_chartista: str | None
    fuentes: list[str]
    """`external_id` de eventos reales del catálogo (`eventos.external_id`,
    p. ej. "ripple-sec-case-ends") que sustentan la tesis — nunca un id
    inventado para datos de mercado (no existe ese tipo de evidencia
    direccionable todavía). `app.ai.agents.consolidator` verifica cada uno
    contra la base de datos antes de aceptar la respuesta."""


class ConsolidadorOutput(_AgentOutputBase):
    recomendacion: Literal["invertir", "no_invertir"]
    razon_no_invertir: str | None
    opciones: list[OpcionConsolidada] = Field(max_length=5)
    """Máximo 5 (sección 7.2/19 del plan: hasta 3 "principal" + hasta 2
    "menor_riesgo"). Puede tener menos, o ninguna — "no fuerces cinco
    opciones para llenar un formato" (prompt del agente)."""
    confianza_global: Literal["alta", "media", "baja"]

    @model_validator(mode="after")
    def _validar_coherencia(self) -> "ConsolidadorOutput":
        if self.recomendacion == "no_invertir" and not self.razon_no_invertir:
            raise ValueError('"razon_no_invertir" es obligatorio cuando recomendacion es "no_invertir"')
        if self.recomendacion == "invertir" and not self.opciones:
            raise ValueError('recomendacion "invertir" sin ninguna opción en "opciones" es incoherente')

        principales = [o for o in self.opciones if o.categoria == "principal"]
        menor_riesgo = [o for o in self.opciones if o.categoria == "menor_riesgo"]
        if len(principales) > 3:
            raise ValueError(f'máximo 3 opciones "principal" (sección 7.2 del plan), llegaron {len(principales)}')
        if len(menor_riesgo) > 2:
            raise ValueError(
                f'máximo 2 opciones "menor_riesgo" (sección 7.2 del plan), llegaron {len(menor_riesgo)}'
            )
        return self


# ---------------------------------------------------------------------------
# Agente 1 — Entrevistador (Paso 10b)
# ---------------------------------------------------------------------------


class PerfilUsuarioOutput(_AgentOutputBase):
    """Reproduce el bloque "SALIDA" del Agente 1 (sección 7.2 del plan). Los
    rangos son los mismos que las restricciones de la tabla `perfil_usuario`:
    una respuesta fuera de rango es un fallo de schema, nunca se corrige."""

    horizonte_meses: int = Field(gt=0)
    perdida_maxima_pct: float = Field(ge=0, le=100)
    experiencia: Literal["ninguna", "basica", "intermedia", "avanzada"]
    activos_excluidos: list[str]
    tolerancia_declarada: str
    objetivo_rentabilidad_pct: float = Field(ge=0)
    objetivo_es_realista: bool
    disponibilidad_aprobacion: str
    pct_patrimonio: float = Field(ge=0, le=100)
    contradicciones_detectadas: list[str]
    confianza_perfil: Literal["alta", "media", "baja"]
