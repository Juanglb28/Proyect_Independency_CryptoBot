"""`alertas`: persistencia, inmutabilidad en la base y envío con un canal falso."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.domain.enums import TipoAlerta
from app.notifications.dispatcher import MAX_INTENTOS_ENTREGA, dispatch_pending_alerts
from app.notifications.telegram import TelegramError
from app.repositories.alertas import (
    AlertaRow,
    append_alerta,
    claim_siguiente_pendiente,
    get_ultima_alerta_de_tipos,
    list_alertas,
    record_entrega_exitosa,
    record_entrega_fallida,
)

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def _row(clave="k1", tipo=TipoAlerta.CAIDA_1H, mensaje="msg") -> AlertaRow:
    return AlertaRow(tipo=tipo, subject="ETHUSDT", dedup_key=clave, occurred_at=T0, message=mensaje)


class Canal:
    def __init__(self, fallos=0):
        self.enviados, self.fallos = [], fallos

    def send(self, texto):
        if self.fallos:
            self.fallos -= 1
            raise TelegramError("sin red")
        self.enviados.append(texto)


@pytest.fixture
def fabrica(db_session):
    """Un `sessionmaker` falso que reparte la sesión de prueba (su transacción se descarta al final)."""
    from contextlib import contextmanager

    class _F:
        def __call__(self):
            return db_session

    import app.notifications.dispatcher as d

    @contextmanager
    def _scope(factory):
        yield db_session
        db_session.flush()

    original = d.session_scope
    d.session_scope = _scope
    yield _F()
    d.session_scope = original


def test_append_es_idempotente_por_dedup_key(db_session):
    assert append_alerta(db_session, _row("a")) is True
    assert append_alerta(db_session, _row("a", mensaje="otro texto")) is False  # mismo episodio: no se duplica
    assert append_alerta(db_session, _row("b")) is True
    assert [a.dedup_key for a in list_alertas(db_session)] == ["b", "a"]


def test_el_contenido_no_se_modifica_ni_se_borra(db_session):
    append_alerta(db_session, _row("a"))
    for sentencia in ("UPDATE alertas SET message = 'otro'", "UPDATE alertas SET tipo = 'PAUSA_DRAWDOWN'",
                      "UPDATE alertas SET dedup_key = 'z'", "DELETE FROM alertas"):
        with pytest.raises(DBAPIError, match="alertas"):
            with db_session.begin_nested():
                db_session.execute(text(sentencia))


def test_el_recibo_solo_avanza(db_session):
    append_alerta(db_session, _row("a"))
    alerta = claim_siguiente_pendiente(db_session, max_intentos=5)
    record_entrega_fallida(db_session, alerta.id, error="sin red")
    db_session.refresh(alerta)
    assert (alerta.delivery_attempts, alerta.delivered_at, alerta.last_delivery_error) == (1, None, "sin red")
    record_entrega_exitosa(db_session, alerta.id, now=T0)
    db_session.refresh(alerta)
    assert (alerta.delivery_attempts, alerta.delivered_at, alerta.last_delivery_error) == (2, T0, None)
    for sentencia in ("UPDATE alertas SET delivered_at = NULL", "UPDATE alertas SET delivery_attempts = 0",
                      "UPDATE alertas SET delivered_at = now()"):
        with pytest.raises(DBAPIError, match="alertas"):
            with db_session.begin_nested():
                db_session.execute(text(sentencia))
    assert claim_siguiente_pendiente(db_session, max_intentos=5) is None  # ya entregada


def test_estado_por_ultimo_tipo_no_por_reloj(db_session):
    append_alerta(db_session, _row("c", TipoAlerta.BINANCE_CAIDO))
    append_alerta(db_session, _row("r", TipoAlerta.BINANCE_RESTABLECIDO))
    par = [TipoAlerta.BINANCE_CAIDO, TipoAlerta.BINANCE_RESTABLECIDO]
    assert get_ultima_alerta_de_tipos(db_session, par).tipo == TipoAlerta.BINANCE_RESTABLECIDO
    assert get_ultima_alerta_de_tipos(db_session, [TipoAlerta.CAIDA_1H]) is None


def test_despacho_envia_en_orden_y_no_reenvia(db_session, fabrica):
    for k in ("a", "b", "c"):
        append_alerta(db_session, _row(k, mensaje=f"m-{k}"))
    canal = Canal()
    r = dispatch_pending_alerts(fabrica, canal, now=T0)
    assert (r.enviadas, r.fallidas, canal.enviados) == (3, 0, ["m-a", "m-b", "m-c"])
    assert dispatch_pending_alerts(fabrica, canal, now=T0).enviadas == 0  # nada se reenvía
    assert len(canal.enviados) == 3


def test_un_fallo_se_anota_se_reintenta_y_no_bloquea_a_los_demas(db_session, fabrica):
    append_alerta(db_session, _row("a", mensaje="m-a"))
    append_alerta(db_session, _row("b", mensaje="m-b"))
    canal = Canal(fallos=1)  # falla el primero
    r = dispatch_pending_alerts(fabrica, canal, now=T0)
    assert (r.enviadas, r.fallidas, canal.enviados) == (1, 1, ["m-b"])
    por_clave = {a.dedup_key: a for a in list_alertas(db_session)}
    assert por_clave["a"].delivery_attempts == 1 and por_clave["a"].last_delivery_error == "sin red"
    assert dispatch_pending_alerts(fabrica, canal, now=T0).enviadas == 1  # la siguiente llamada reintenta
    assert canal.enviados == ["m-b", "m-a"]


def test_deja_de_reintentar_al_agotar_los_intentos(db_session, fabrica):
    append_alerta(db_session, _row("a"))
    canal = Canal(fallos=100)
    for _ in range(MAX_INTENTOS_ENTREGA + 3):
        dispatch_pending_alerts(fabrica, canal, now=T0)
    (alerta,) = list_alertas(db_session)
    assert alerta.delivery_attempts == MAX_INTENTOS_ENTREGA and alerta.delivered_at is None
    assert alerta.last_delivery_error == "sin red"  # queda visible por qué no llegó


def test_sin_canal_los_avisos_esperan_en_la_tabla(db_session, fabrica):
    append_alerta(db_session, _row("a"))
    assert dispatch_pending_alerts(fabrica, None, now=T0).enviadas == 0
    (alerta,) = list_alertas(db_session)
    assert alerta.delivery_attempts == 0 and alerta.delivered_at is None
