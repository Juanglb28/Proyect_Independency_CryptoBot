"""`scripts.monitor` y los avisos (Paso 11a-2): se envían DESPUÉS del chequeo,
con un canal falso, y ningún fallo de Telegram detiene al monitor."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import text

import app.notifications.dispatcher as dispatcher_module
import scripts.monitor as monitor_script
from app.domain.enums import TipoAlerta
from app.notifications.telegram import TelegramError
from app.repositories.alertas import list_alertas
from app.repositories.positions import list_open_posiciones
from scripts.monitor import run_once
from tests.integration.test_escalations import _entrada_pendiente
from tests.integration.test_monitor import CAPITAL, FakePrices, _abrir, _sembrar_pico, _t


class Canal:
    def __init__(self, fallos=0):
        self.enviados, self.fallos = [], fallos

    def send(self, texto):
        if self.fallos:
            self.fallos -= 1
            raise TelegramError("sin red")
        self.enviados.append(texto)


@pytest.fixture()
def usar_db_session(db_session, monkeypatch):
    @contextmanager
    def _scope(_factory):
        yield db_session
        db_session.flush()

    monkeypatch.setattr(monitor_script, "session_scope", _scope)
    monkeypatch.setattr(dispatcher_module, "session_scope", _scope)
    return db_session


def _run(precios, minutos, sender):
    return run_once(None, price_source=FakePrices(precios), initial_capital=CAPITAL, now=_t(minutos), sender=sender)


def test_el_aviso_se_envia_despues_del_chequeo_y_queda_entregado(usar_db_session):
    _entrada_pendiente(usar_db_session)
    canal = Canal()
    lineas, codigo = _run({}, 1, canal)
    assert codigo == 0 and len(canal.enviados) == 1 and "APROBACIÓN PENDIENTE" in canal.enviados[0]
    assert "AVISO registrado: ORDEN_PENDIENTE_APROBACION" in "\n".join(lineas)
    assert "Avisos enviados por Telegram: 1" in "\n".join(lineas)
    (alerta,) = list_alertas(usar_db_session)
    assert alerta.delivered_at is not None and alerta.delivery_attempts == 1

    _run({}, 2, canal)  # nada nuevo ni pendiente: no se reenvía
    assert len(canal.enviados) == 1


def test_un_fallo_de_telegram_no_cambia_el_resultado_y_se_reintenta(usar_db_session):
    _entrada_pendiente(usar_db_session)
    canal = Canal(fallos=1)
    lineas, codigo = _run({}, 1, canal)
    assert codigo == 0 and "no se pudieron enviar" in "\n".join(lineas)
    (alerta,) = list_alertas(usar_db_session)
    assert alerta.delivered_at is None and alerta.delivery_attempts == 1 and alerta.last_delivery_error

    _run({}, 2, canal)  # el siguiente chequeo reintenta
    assert len(canal.enviados) == 1 and list_alertas(usar_db_session)[0].delivered_at is not None


def test_sin_telegram_configurado_los_avisos_esperan_en_la_tabla(usar_db_session):
    _entrada_pendiente(usar_db_session)
    lineas, codigo = _run({}, 1, None)
    assert codigo == 0 and "Telegram" not in "\n".join(lineas)
    (alerta,) = list_alertas(usar_db_session)
    assert alerta.delivered_at is None and alerta.delivery_attempts == 0


def test_un_error_inesperado_del_despachador_no_tumba_el_monitor(usar_db_session, monkeypatch):
    def _roto(*_a, **_k):
        raise RuntimeError("falla simulada")

    monkeypatch.setattr(monitor_script, "dispatch_pending_alerts", _roto)
    _entrada_pendiente(usar_db_session)
    lineas, codigo = _run({}, 1, Canal())
    assert codigo == 0 and "el envío falló" in "\n".join(lineas)
    assert len(list_alertas(usar_db_session)) == 1  # el aviso quedó guardado


def test_la_pausa_por_drawdown_llega_por_telegram(usar_db_session):
    _abrir(usar_db_session)
    _sembrar_pico(usar_db_session, "1170", "1170")
    canal = Canal()
    _run({"BTCUSDT": "94.6"}, 1, canal)
    assert any("PAUSA POR DRAWDOWN" in m for m in canal.enviados)


def test_la_discrepancia_se_guarda_y_se_envia_aunque_el_monitor_se_detenga(usar_db_session):
    _abrir(usar_db_session)
    pos = list_open_posiciones(usar_db_session)[0]
    usar_db_session.execute(text("UPDATE posiciones SET remaining_quantity = 0.5 WHERE id = :id"), {"id": pos.id})
    usar_db_session.expire_all()

    canal = Canal()
    lineas, codigo = _run({"BTCUSDT": "50"}, 1, canal)
    assert codigo == 2 and "SIN actuar" in lineas[0]
    (alerta,) = list_alertas(usar_db_session)
    assert alerta.tipo == TipoAlerta.DISCREPANCIA_RECONCILIACION and "CANTIDAD_RESTANTE_NO_COINCIDE" in alerta.message
    assert len(canal.enviados) == 1 and alerta.delivered_at is not None


def test_si_no_se_puede_guardar_el_aviso_de_discrepancia_el_monitor_igual_se_detiene(usar_db_session, monkeypatch):
    def _roto(*_a, **_k):
        raise RuntimeError("falla simulada")

    monkeypatch.setattr(monitor_script, "avisar_discrepancia", _roto)
    _abrir(usar_db_session)
    pos = list_open_posiciones(usar_db_session)[0]
    usar_db_session.execute(text("UPDATE posiciones SET remaining_quantity = 0.5 WHERE id = :id"), {"id": pos.id})
    usar_db_session.expire_all()
    _, codigo = _run({"BTCUSDT": "50"}, 1, Canal())
    assert codigo == 2
