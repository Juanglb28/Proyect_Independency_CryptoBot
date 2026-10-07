"""Envía UN mensaje de prueba por Telegram para comprobar el token y el chat.

No toca la base de datos ni ninguna alerta: es solo para verificar la
configuración (`TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en el `.env`). Nunca
imprime el token.

Uso:
    docker compose run --rm app python -m scripts.telegram_test
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from app.config import get_settings
from app.notifications.telegram import Sender, TelegramError, build_sender

MENSAJE = "Prueba de avisos: si lees esto, Telegram está bien configurado. (Plataforma de trading, paper)"


def run(sender: Sender | None, emit: Callable[[str], None] = print) -> int:
    if sender is None:
        emit("Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en el .env. No se envió nada.")
        return 2
    try:
        sender.send(MENSAJE)
    except TelegramError as exc:
        emit(f"No se pudo enviar: {exc}")
        return 1
    emit("Mensaje enviado. Revisa tu Telegram.")
    return 0


def main() -> int:
    cfg = get_settings()
    return run(build_sender(cfg.telegram_bot_token, cfg.telegram_chat_id))


if __name__ == "__main__":
    sys.exit(main())
