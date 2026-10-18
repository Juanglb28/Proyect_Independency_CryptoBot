"""Script `scripts.estudio_velas_1m`: argumentos, informe y que sin datos de 1 minuto no cambia nada."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.data.backtest_study import observaciones_de_simbolo
from app.data.exit_simulation import MAXIMO_PRIMERO, MINIMO_PRIMERO
from app.data.minute_candles import ResolvedorMinutos
from scripts import estudio_velas_1m as script
from tests.unit.test_estudio_orden_intravela_script import _serie


def test_un_simbolo_fuera_de_la_lista_autorizada_se_rechaza_sin_tocar_la_base(capsys):
    assert script.main(["--symbols", "DOGEFAKEUSDT"]) == 1
    assert "no está en la lista autorizada" in capsys.readouterr().err


def _serie_con_vela_violenta():
    """Serie sintética con UNA vela de 1h que toca stop y objetivo a la vez (clase `stop_y_objetivo`)."""
    diarias, horarias = _serie(300, 5)
    i = 250 * 24  # hora 0 del día 250: la primera vela posterior a la entrada de las 23:00 del día 249
    c = horarias[i - 1]["close"]
    horarias[i].update(open=c, high=c * Decimal("1.15"), low=c * Decimal("0.85"), close=c)
    return diarias, horarias, horarias[i]["open_time"]


def test_sin_datos_de_1_minuto_la_lectura_resuelta_es_identica_a_la_publicada(tmp_path):
    diarias, horarias, hora_violenta = _serie_con_vela_violenta()
    r = ResolvedorMinutos(directorio=tmp_path, descargar=False)  # modo conteo: nunca devuelve minutos
    publicada = observaciones_de_simbolo("X", diarias, horarias)
    con_resolvedor = observaciones_de_simbolo("X", diarias, horarias, resolvedor=r)
    assert con_resolvedor == publicada
    assert ("X", hora_violenta) in r.velas_ambiguas_unicas()  # se detecta la vela violenta
    assert r.por_clase().get("stop_y_objetivo", 0) >= 1
    assert set(r.motivos_no_resueltas()) == {"pendiente_de_descargar"}


def test_el_informe_se_imprime_y_declara_la_cobertura(tmp_path, capsys):
    diarias, horarias = _serie(300, 5)
    r = ResolvedorMinutos(directorio=tmp_path, descargar=False)
    base = observaciones_de_simbolo("X", diarias, horarias, orden_intravela=MAXIMO_PRIMERO)
    inversa = observaciones_de_simbolo("X", diarias, horarias, orden_intravela=MINIMO_PRIMERO)
    resuelta = observaciones_de_simbolo("X", diarias, horarias, resolvedor=r)
    script.imprimir(r, base, inversa, resuelta)
    salida = capsys.readouterr().out
    for esperado in (
        "RESOLUCIÓN DE LAS VELAS AMBIGUAS", "COBERTURA", "SIN resolver", "Dónde cae el desenlace real",
        "Real ENTRE las dos lecturas", "REAL (1 minuto)", "Veredicto walk-forward",
    ):
        assert esperado in salida


def test_el_modo_solo_contar_informa_cuantas_velas_hay_que_descargar(tmp_path, capsys):
    diarias, horarias = _serie(300, 5)
    r = ResolvedorMinutos(directorio=tmp_path, descargar=False)
    base = observaciones_de_simbolo("X", diarias, horarias)
    observaciones_de_simbolo("X", diarias, horarias, resolvedor=r)
    script.imprimir_solo_contar(r, base)
    salida = capsys.readouterr().out
    assert "CONTEO" in salida and "Por descargar" in salida and "Cota inferior" in salida


class _BinanceFalso:
    """Entrega, para cualquier hora de la serie, 60 minutos que agregan EXACTAMENTE la vela de 1h
    (el máximo en el minuto 10 y el mínimo en el minuto 20: «máximo primero»)."""

    def __init__(self, horarias):
        self._por_hora = {int(h["open_time"].timestamp() * 1000): h for h in horarias}
        self.llamadas = 0

    def fetch_ohlcv(self, simbolo, timeframe, limit, since):
        self.llamadas += 1
        h = self._por_hora[since]
        o, alto, bajo, c = (float(h[k]) for k in ("open", "high", "low", "close"))
        filas = []
        for k in range(60):
            if k == 0:
                f = (o, o, o, o)
            elif k < 10:
                f = (o, o, o, o)
            elif k == 10:
                f = (o, alto, o, alto)
            elif k < 20:
                f = (alto, alto, alto, alto)
            elif k == 20:
                f = (alto, alto, bajo, bajo)
            elif k < 59:
                f = (bajo, bajo, bajo, bajo)
            else:
                f = (bajo, max(bajo, c), min(bajo, c), c)
            filas.append([since + k * 60_000, f[0], f[1], f[2], f[3], 1.0])
        return filas


def test_con_minutos_validos_la_vela_violenta_se_resuelve_y_cambia_el_desenlace(tmp_path):
    diarias, horarias, hora_violenta = _serie_con_vela_violenta()
    for c in (*diarias, *horarias):
        c["symbol"] = "BTCUSDT"  # el resolvedor solo atiende símbolos de la lista autorizada
    for c in horarias:  # precios con 2 decimales: ida y vuelta exacta por float, como los de Binance
        for k in ("open", "high", "low", "close"):
            c[k] = c[k].quantize(Decimal("0.01"))
    falso = _BinanceFalso(horarias)
    r = ResolvedorMinutos(directorio=tmp_path, exchange=falso)
    base = observaciones_de_simbolo("BTCUSDT", diarias, horarias)
    resuelta = observaciones_de_simbolo("BTCUSDT", diarias, horarias, resolvedor=r)
    assert not r.no_resueltas and falso.llamadas == len(r.velas_ambiguas_unicas()) >= 1
    por_dia = {o.dia: o for o in base}
    cambian = [o for o in resuelta if o.retorno_neto != por_dia[o.dia].retorno_neto]
    assert cambian, "la vela violenta debía cambiar el desenlace de al menos una entrada"
    for o in cambian:
        assert o.score == por_dia[o.dia].score  # solo cambia el desenlace, nunca el score
    # Antes: STOP (la regla de 1h). Con el objetivo primero: cierre parcial + trailing, mejor que el stop.
    dia = (hora_violenta - timedelta(hours=1)).date() + timedelta(days=1)
    antes, despues = por_dia[dia], next(o for o in resuelta if o.dia == dia)
    assert antes.retorno_neto < 0 < despues.retorno_neto
    script.imprimir(r, base, base, resuelta)  # el informe también funciona con datos resueltos
