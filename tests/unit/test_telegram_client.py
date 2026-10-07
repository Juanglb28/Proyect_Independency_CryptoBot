"""Cliente de Telegram con un `opener` falso: ninguna prueba toca la red."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from app.notifications.telegram import LIMITE_CARACTERES, TelegramClient, TelegramError, build_sender

TOKEN = "123456:ABC-token-secreto"
CHAT = "987654321"


class _Resp:
    def __init__(self, cuerpo: dict):
        self._b = json.dumps(cuerpo).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _cliente(opener):
    return TelegramClient(TOKEN, CHAT, opener=opener)


def test_envia_texto_plano_al_chat_configurado():
    vistos = []

    def opener(peticion, timeout):
        vistos.append((peticion.full_url, json.loads(peticion.data), peticion.get_method(), timeout))
        return _Resp({"ok": True})

    _cliente(opener).send("hola")
    url, cuerpo, metodo, timeout = vistos[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage" and metodo == "POST" and timeout == 10.0
    assert cuerpo == {"chat_id": CHAT, "text": "hola", "disable_web_page_preview": True}
    assert "parse_mode" not in cuerpo  # nada se interpreta como formato


def test_recorta_a_4096_caracteres():
    vistos = []
    _cliente(lambda p, timeout: (vistos.append(json.loads(p.data)), _Resp({"ok": True}))[1]).send("x" * 10_000)
    assert len(vistos[0]["text"]) == LIMITE_CARACTERES


@pytest.mark.parametrize("fallo", [
    urllib.error.HTTPError(f"https://api.telegram.org/bot{TOKEN}/sendMessage", 401, "No", {}, io.BytesIO(
        json.dumps({"ok": False, "description": f"Unauthorized {TOKEN}"}).encode())),
    urllib.error.URLError(f"fallo {TOKEN} {CHAT}"),
    TimeoutError(f"tiempo agotado {TOKEN}"),
    OSError(f"red caída {CHAT}"),
])
def test_los_errores_nunca_contienen_token_ni_chat(fallo):
    def opener(peticion, timeout):
        raise fallo

    with pytest.raises(TelegramError) as exc:
        _cliente(opener).send("hola")
    assert TOKEN not in str(exc.value) and CHAT not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__  # la causa (con la URL) no se encadena


def test_respuesta_ok_falso_o_no_json_es_error():
    with pytest.raises(TelegramError, match="rechazó"):
        _cliente(lambda p, timeout: _Resp({"ok": False, "description": "chat not found"})).send("x")

    class Basura(_Resp):
        def read(self):
            return b"<html>"

    with pytest.raises(TelegramError, match="JSON"):
        _cliente(lambda p, timeout: Basura({})).send("x")


def test_repr_no_muestra_el_token_y_hace_falta_token_y_chat():
    assert TOKEN not in repr(_cliente(lambda *a, **k: None)) and CHAT not in repr(_cliente(lambda *a, **k: None))
    with pytest.raises(ValueError):
        TelegramClient("", CHAT)


def test_build_sender_sin_configuracion_devuelve_none():
    assert build_sender(None, CHAT) is None and build_sender(TOKEN, None) is None and build_sender("", "") is None
    assert isinstance(build_sender(TOKEN, CHAT), TelegramClient)


def test_script_de_prueba_informa_sin_exponer_el_token():
    from scripts.telegram_test import MENSAJE, run

    salida = []
    assert run(None, salida.append) == 2 and "Falta" in salida[0]

    class Ok:
        def send(self, t):
            self.t = t

    ok = Ok()
    assert run(ok, salida.append) == 0 and ok.t == MENSAJE

    class Mal:
        def send(self, t):
            raise TelegramError("Telegram rechazó el mensaje: chat not found")

    salida.clear()
    assert run(Mal(), salida.append) == 1 and "chat not found" in salida[0]
