"""Reconciliación de v1 (`app/execution/reconciliation.find_discrepancies`):
función pura que verifica que `posiciones` y `ordenes` cuenten la misma
historia. Cada código de discrepancia tiene su caso."""

import uuid
from decimal import Decimal

from app.domain.enums import EstadoPosicion, IntencionOrden, LadoOrden
from app.execution.reconciliation import (
    CodigoDiscrepancia,
    OrdenParaConciliar,
    PosicionParaConciliar,
    find_discrepancies,
)

D = Decimal


def _pos(**kw) -> PosicionParaConciliar:
    base = dict(
        id=uuid.uuid4(),
        symbol="BTCUSDT",
        status=EstadoPosicion.OPEN,
        entry_price=D("100"),
        initial_quantity=D("1"),
        remaining_quantity=D("1"),
        take_profit_hit=False,
    )
    base.update(kw)
    return PosicionParaConciliar(**base)


def _orden(pos, intencion=IntencionOrden.ENTRY, side=LadoOrden.BUY, qty="1", price="100", **kw) -> OrdenParaConciliar:
    base = dict(
        id=uuid.uuid4(),
        position_id=pos.id,
        symbol=pos.symbol,
        side=side,
        intencion=intencion,
        quantity=D(qty),
        execution_price=D(price),
    )
    base.update(kw)
    return OrdenParaConciliar(**base)


def _codigos(posiciones, ordenes, **kw):
    return [d.codigo for d in find_discrepancies(posiciones, ordenes, **kw)]


def test_estado_vacio_cuadra():
    assert find_discrepancies([], []) == []


def test_posicion_abierta_sana_cuadra():
    p = _pos()
    assert _codigos([p], [_orden(p)]) == []


def test_posicion_con_tp_parcial_sana_cuadra():
    p = _pos(remaining_quantity=D("0.5"), take_profit_hit=True)
    ordenes = [
        _orden(p),
        _orden(p, IntencionOrden.TAKE_PROFIT_PARTIAL, LadoOrden.SELL, qty="0.5", price="112"),
    ]
    assert _codigos([p], ordenes) == []


def test_posicion_cerrada_sana_cuadra():
    p = _pos(status=EstadoPosicion.CLOSED, remaining_quantity=D("0"))
    ordenes = [_orden(p), _orden(p, IntencionOrden.STOP_LOSS, LadoOrden.SELL, qty="1", price="92")]
    assert _codigos([p], ordenes) == []


def test_posicion_sin_orden_de_entrada():
    assert _codigos([_pos()], []) == [CodigoDiscrepancia.SIN_ORDEN_DE_ENTRADA]


def test_entradas_multiples():
    p = _pos()
    assert CodigoDiscrepancia.ENTRADAS_MULTIPLES in _codigos([p], [_orden(p), _orden(p)])


def test_entrada_que_no_coincide_con_la_posicion():
    p = _pos()
    assert _codigos([p], [_orden(p, qty="2")]) == [CodigoDiscrepancia.ENTRADA_NO_COINCIDE]
    assert _codigos([p], [_orden(p, price="99")]) == [CodigoDiscrepancia.ENTRADA_NO_COINCIDE]


def test_cantidad_restante_que_no_se_explica_con_las_ventas():
    p = _pos(remaining_quantity=D("0.5"))  # nadie vendió nada
    assert _codigos([p], [_orden(p)]) == [CodigoDiscrepancia.CANTIDAD_RESTANTE_NO_COINCIDE]


def test_venta_que_la_posicion_no_refleja():
    p = _pos()  # sigue con 1 pero hay una venta de 1
    ordenes = [_orden(p), _orden(p, IntencionOrden.STOP_LOSS, LadoOrden.SELL, qty="1", price="92")]
    assert _codigos([p], ordenes) == [CodigoDiscrepancia.CANTIDAD_RESTANTE_NO_COINCIDE]


def test_tp_parcial_marcado_sin_su_venta():
    p = _pos(take_profit_hit=True)
    assert _codigos([p], [_orden(p)]) == [CodigoDiscrepancia.TP_PARCIAL_NO_COINCIDE]


def test_venta_de_tp_parcial_sin_marca_en_la_posicion():
    p = _pos(remaining_quantity=D("0.5"))
    ordenes = [
        _orden(p),
        _orden(p, IntencionOrden.TAKE_PROFIT_PARTIAL, LadoOrden.SELL, qty="0.5", price="112"),
    ]
    assert _codigos([p], ordenes) == [CodigoDiscrepancia.TP_PARCIAL_NO_COINCIDE]


def test_orden_de_otro_simbolo():
    p = _pos()
    assert _codigos([p], [_orden(p), _orden(p, IntencionOrden.OTHER, LadoOrden.SELL, qty="0", symbol="ETHUSDT")]) == [
        CodigoDiscrepancia.ORDEN_DE_OTRO_SIMBOLO
    ]


def test_posicion_abierta_de_un_simbolo_fuera_de_la_lista():
    p = _pos(symbol="FOOUSDT")
    assert _codigos([p], [_orden(p)]) == [CodigoDiscrepancia.SIMBOLO_NO_PERMITIDO]


def test_posicion_cerrada_de_un_simbolo_fuera_de_la_lista_no_molesta():
    p = _pos(symbol="FOOUSDT", status=EstadoPosicion.CLOSED, remaining_quantity=D("0"))
    ordenes = [_orden(p), _orden(p, IntencionOrden.STOP_LOSS, LadoOrden.SELL, qty="1", price="92")]
    assert _codigos([p], ordenes) == []


def test_orden_ejecutada_sin_posicion():
    p = _pos()
    huerfana = OrdenParaConciliar(
        uuid.uuid4(), None, "ETHUSDT", LadoOrden.BUY, IntencionOrden.ENTRY, D("1"), D("100")
    )
    assert _codigos([p], [_orden(p), huerfana]) == [CodigoDiscrepancia.ORDEN_EJECUTADA_SIN_POSICION]


def test_se_reportan_todas_las_discrepancias_no_solo_la_primera():
    a, b = _pos(symbol="BTCUSDT"), _pos(symbol="ETHUSDT", remaining_quantity=D("0.5"))
    codigos = _codigos([a, b], [_orden(b)])  # a sin entrada; b con restante inexplicado
    assert codigos == [CodigoDiscrepancia.SIN_ORDEN_DE_ENTRADA, CodigoDiscrepancia.CANTIDAD_RESTANTE_NO_COINCIDE]
