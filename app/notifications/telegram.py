"""Cliente de Telegram, solo saliente (decisión D6) y sin dependencias nuevas
(decisión D8: `urllib` de la librería estándar).

Seguridad:
- La URL de la API lleva el token del bot. Nunca se incluye en un error, un
  log ni el `repr` del cliente: los mensajes de error se limpian.
- Se envía texto plano (sin `parse_mode`): nada de lo que contenga el aviso se
  interpreta como formato.
- Solo envía al `chat_id` configurado. No recibe nada de Telegram (no hay
  comandos entrantes: aprobar/rechazar sigue siendo manual en la interfaz).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Protocol

_API = "https://api.telegram.org/bot{token}/sendMessage"
LIMITE_CARACTERES = 4096  # tope de Telegram por mensaje
TIMEOUT_SEGUNDOS = 10.0


class TelegramError(Exception):
    """No se pudo enviar. El mensaje nunca contiene el token."""


class Sender(Protocol):
    """Lo único que el despachador necesita de un canal de envío."""

    def send(self, text: str) -> None: ...


class TelegramClient:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        timeout: float = TIMEOUT_SEGUNDOS,
        opener: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        if not token or not chat_id:
            raise ValueError("Hacen falta el token del bot y el chat_id.")
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout
        self._opener = opener

    def __repr__(self) -> str:  # nunca muestra el token
        return "TelegramClient(token=***, chat_id=***)"

    def _limpiar(self, texto: str) -> str:
        return texto.replace(self._token, "***").replace(self._chat_id, "***")

    def send(self, text: str) -> None:
        cuerpo = json.dumps(
            {"chat_id": self._chat_id, "text": text[:LIMITE_CARACTERES], "disable_web_page_preview": True}
        ).encode("utf-8")
        peticion = urllib.request.Request(
            _API.format(token=self._token),
            data=cuerpo,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(peticion, timeout=self._timeout) as respuesta:  # type: ignore[operator]
                datos = json.loads(respuesta.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detalle = ""
            try:
                detalle = json.loads(exc.read().decode("utf-8")).get("description", "")
            except Exception:  # noqa: BLE001 - el detalle es opcional
                pass
            raise TelegramError(self._limpiar(f"Telegram respondió HTTP {exc.code}: {detalle}".strip(": "))) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramError(self._limpiar(f"No se pudo contactar a Telegram ({type(exc).__name__})")) from None
        except (ValueError, UnicodeDecodeError):
            raise TelegramError("Telegram devolvió una respuesta que no es JSON válido.") from None
        if not isinstance(datos, dict) or datos.get("ok") is not True:
            descripcion = datos.get("description", "sin descripción") if isinstance(datos, dict) else "respuesta inesperada"
            raise TelegramError(self._limpiar(f"Telegram rechazó el mensaje: {descripcion}"))


def build_sender(token: str | None, chat_id: str | None) -> TelegramClient | None:
    """Cliente listo, o `None` si falta alguno de los dos datos (los avisos se
    quedan guardados en `alertas` y se envían cuando se configure)."""
    if not token or not chat_id:
        return None
    return TelegramClient(token, chat_id)
