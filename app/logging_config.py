"""Logging estructurado (JSON), sin dependencias externas.

Regla del proyecto (sección 24 del master prompt): nunca registrar API keys,
secretos, tokens ni credenciales. Este módulo no filtra el contenido de los
mensajes por sí solo — eso es responsabilidad de quien llama a logger.info/
error/etc. Nunca pasar `settings` completo ni cualquier campo `*_api_key` /
`*_secret` / `*_token` a un log.

Cada evento importante debe poder incluir contexto de correlación (ciclo,
componente, operación) vía el parámetro `extra` de logging estándar; este
formatter los serializa automáticamente si están presentes.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

# Campos estándar de un LogRecord que NO deben repetirse como "extra" en el JSON.
_STANDARD_LOG_RECORD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys())


class JsonFormatter(logging.Formatter):
    """Formatea cada línea de log como un objeto JSON de una sola línea."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Cualquier campo pasado vía `extra={...}` se incluye tal cual, para
        # dar contexto de correlación (ciclo, componente, operación, etc.)
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_RECORD_FIELDS and key not in payload:
                payload[key] = value

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Configura el logging raíz de la aplicación en formato JSON hacia stdout.

    Se llama una única vez, al arrancar la aplicación (en `main.py`), nunca
    dentro de un módulo importado, para evitar handlers duplicados.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Evita duplicar handlers si configure_logging() se llama más de una vez
    # (por ejemplo, en tests que arrancan la app varias veces en el mismo proceso).
    root_logger.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    root_logger.addHandler(handler)
