"""Interpretación del latido (heartbeat) del scheduler — regla pura (2026-10-12).

El scheduler escribe cada 5 minutos un JSON con ``status``, ``timestamp`` (ISO
8601 en UTC) y ``pid``. Decidir si ese latido está *vencido* (demasiado viejo)
es una decisión determinista que no depende de Streamlit, la base ni la red, así
que vive aquí (P1): una sola definición de "vencido" compartida por quien lo lea
(hoy la vista de sistema; el vigía local de Windows mira el mismo archivo con la
misma regla, reescrita en PowerShell porque corre fuera de Docker).

Esta función NO lee archivos: recibe el texto crudo (o ``None`` si no se pudo
leer) para mantenerse pura y testeable. El llamador se encarga de la E/S.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

UMBRAL_LATIDO_VENCIDO_MINUTOS = 15
"""El scheduler late cada 5 min; 15 min sin latido (tres latidos perdidos) es
señal de que algo anda mal. Umbral compartido con el vigía de PowerShell: si se
cambia aquí, cambiarlo también en ``scripts/scheduler_watchdog.ps1``."""


@dataclass(frozen=True)
class LecturaLatido:
    """Resultado de interpretar el contenido del archivo de latido."""

    disponible: bool
    """``True`` si el texto existía y tenía un ``timestamp`` válido."""
    timestamp: datetime | None
    pid: int | None
    edad: timedelta | None
    """``now - timestamp`` (``None`` si no hay timestamp)."""
    vencido: bool
    """``True`` si no se pudo leer o el latido supera el umbral."""


def interpretar_latido(
    texto: str | None,
    *,
    now: datetime,
    umbral_minutos: int = UMBRAL_LATIDO_VENCIDO_MINUTOS,
) -> LecturaLatido:
    """Interpreta el texto del archivo de latido.

    - ``texto is None`` (no se pudo leer): no disponible, vencido.
    - JSON inválido, sin ``timestamp`` o con fecha no parseable: no disponible,
      vencido.
    - Correcto: disponible, con la edad y si supera el umbral.

    Un latido sin fecha o ilegible se trata como vencido a propósito: ante la
    duda, avisar.
    """
    if texto is None:
        return LecturaLatido(disponible=False, timestamp=None, pid=None, edad=None, vencido=True)
    try:
        data = json.loads(texto)
        ts_str = data.get("timestamp") if isinstance(data, dict) else None
        if ts_str is None:
            return LecturaLatido(disponible=False, timestamp=None, pid=None, edad=None, vencido=True)
        ts = datetime.fromisoformat(ts_str)
        pid = data.get("pid")
        edad = now - ts
        return LecturaLatido(
            disponible=True,
            timestamp=ts,
            pid=int(pid) if pid is not None else None,
            edad=edad,
            vencido=edad > timedelta(minutes=umbral_minutos),
        )
    except (json.JSONDecodeError, ValueError, TypeError):
        return LecturaLatido(disponible=False, timestamp=None, pid=None, edad=None, vencido=True)
