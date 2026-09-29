"""Schemas Pydantic de la salida de cada agente (sección 7.2 del plan,
Paso 6 — "Schemas Pydantic, cliente Anthropic, agentes y registro de
llamadas_ia").

Cada schema reproduce EXACTAMENTE el bloque "SALIDA" del prompt
correspondiente en el plan — mismos nombres de campo (en español, tal como
están escritos ahí), mismos valores permitidos en los campos enumerados.
No se renombra nada a inglés ni se "mejora" la forma del JSON: cambiar esto
aquí sin cambiar el prompt real haría que la validación rechazara
respuestas correctas del modelo.

TODO — DECISIÓN PENDIENTE: el Agente 5 (Consolidador) no tiene un schema
aquí. La sección 7.2 del plan describe su prompt (prohibiciones, esquema de
ponderación, "tres opciones... y dos opciones adicionales", cada una con
"un escenario adverso concreto") pero, a diferencia de los Agentes 2/3/4,
nunca llega a mostrar un bloque JSON de salida explícito. Inventar esa
forma ahora — nombres de campo para cada "opción", cómo se representa el
score final, el escenario adverso, etc. — sería precisamente el tipo de
decisión arbitraria escondida en el código que el proyecto prohíbe
(sección 30/15.11), más aún porque esa forma probablemente determina
directamente las columnas de `propuestas` y el `decision_snapshot` (Paso 7,
secciones 6 y 14). Queda pendiente hasta que se defina explícitamente.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


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
