"""Construcción del contexto de Kronos: ausencia de look-ahead y datos faltantes (E6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.experiments.kronos.contexto import ContextoKronos, MotivoSinContexto, construir_contexto

D = Decimal
H = timedelta(hours=1)
AS_OF = datetime(2025, 3, 1, 0, 0, tzinfo=timezone.utc)


def _vela(abre, close="100", cerrada=True, volumen="5"):
    return {
        "symbol": "BTCUSDT", "timeframe": "1h", "open_time": abre, "open": D(close), "high": D(close),
        "low": D(close), "close": D(close), "volume": D(volumen), "cerrada": cerrada,
    }


def _serie(n, hasta_abre, close=lambda i: "100"):
    """n velas contiguas cuya ÚLTIMA abre en `hasta_abre`."""
    return [_vela(hasta_abre - H * (n - 1 - i), close(i)) for i in range(n)]


def test_construye_exactamente_lookback_velas_que_terminan_en_as_of():
    velas = _serie(500, AS_OF - H)
    ctx = construir_contexto("BTCUSDT", velas, as_of=AS_OF, lookback=400, horizon=24)
    assert isinstance(ctx, ContextoKronos)
    assert len(ctx.velas) == 400
    assert ctx.velas[-1]["open_time"] == AS_OF - H
    assert ctx.velas[0]["open_time"] == AS_OF - H * 400
    assert ctx.aperturas_a_pronosticar[0] == AS_OF and len(ctx.aperturas_a_pronosticar) == 24
    assert ctx.apertura_de_la_vela_final == AS_OF + H * 23


def test_las_velas_posteriores_a_as_of_nunca_entran_aunque_esten_guardadas():
    """Ausencia de look-ahead: con todo el historial cargado, el contexto es idéntico
    al que se obtiene con la serie truncada en as_of."""
    completa = _serie(700, AS_OF + H * 200, close=lambda i: str(100 + i))
    truncada = [v for v in completa if v["open_time"] + H <= AS_OF]
    a = construir_contexto("BTCUSDT", completa, as_of=AS_OF, lookback=400, horizon=24)
    b = construir_contexto("BTCUSDT", truncada, as_of=AS_OF, lookback=400, horizon=24)
    assert a == b
    assert all(v["open_time"] + H <= AS_OF for v in a.velas)


def test_una_vela_que_cierra_despues_de_as_of_se_ignora_aunque_cerrada_sea_true():
    velas = _serie(450, AS_OF - H) + [_vela(AS_OF, "999")]  # abre en as_of: cierra a as_of+1h
    ctx = construir_contexto("BTCUSDT", velas, as_of=AS_OF, lookback=400, horizon=24)
    assert isinstance(ctx, ContextoKronos) and ctx.ultimo_cierre == D("100")


def test_un_hueco_no_se_rellena_se_descarta():
    velas = [v for v in _serie(500, AS_OF - H) if v["open_time"] != AS_OF - H * 100]
    assert construir_contexto("BTCUSDT", velas, as_of=AS_OF, lookback=400, horizon=24) is MotivoSinContexto.HUECO_EN_LAS_VELAS


def test_pocas_velas_se_descarta():
    assert construir_contexto("BTCUSDT", _serie(399, AS_OF - H), as_of=AS_OF, lookback=400, horizon=24) is MotivoSinContexto.VELAS_INSUFICIENTES


def test_si_falta_la_ultima_vela_no_se_usa_una_anterior():
    velas = _serie(500, AS_OF - H * 2)  # la última disponible abre 2 h antes
    assert construir_contexto("BTCUSDT", velas, as_of=AS_OF, lookback=400, horizon=24) is MotivoSinContexto.ULTIMA_VELA_NO_ES_LA_ESPERADA


def test_vela_no_cerrada_dentro_de_la_ventana_se_rechaza():
    velas = _serie(450, AS_OF - H)
    velas[-1] = _vela(AS_OF - H, cerrada=False)
    assert construir_contexto("BTCUSDT", velas, as_of=AS_OF, lookback=400, horizon=24) is MotivoSinContexto.VELA_NO_CERRADA


def test_precio_no_positivo_se_rechaza():
    velas = _serie(450, AS_OF - H)
    velas[-5] = _vela(AS_OF - H * 5, close="0")
    assert construir_contexto("BTCUSDT", velas, as_of=AS_OF, lookback=400, horizon=24) is MotivoSinContexto.PRECIO_NO_POSITIVO


@pytest.mark.parametrize("as_of", [datetime(2025, 3, 1, 0, 0), datetime(2025, 3, 1, 0, 30, tzinfo=timezone.utc)])
def test_as_of_debe_ser_utc_y_hora_en_punto(as_of):
    with pytest.raises(ValueError):
        construir_contexto("BTCUSDT", _serie(450, AS_OF - H), as_of=as_of, lookback=400, horizon=24)
