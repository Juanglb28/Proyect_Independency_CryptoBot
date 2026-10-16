"""K1: parámetros congelados, fechas fijadas, generación reanudable y unión con el desenlace (E6)."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.data.backtest_study import observaciones_de_simbolo
from app.data.exchange_client import Candle
from app.experiments.kronos import k1
from app.experiments.kronos.k1 import (
    PronosticoGuardado,
    cargar_o_fijar_fechas,
    construir_observaciones_k1,
    fechas_diagnostico,
    fechas_primarias,
    generar_pronosticos,
    huella_protocolo,
    parametros_k1,
    ultima_fecha_posible,
)
from app.experiments.kronos.pesos import LockPesos, PesoFijado

D = Decimal
H = timedelta(hours=1)
REV = "a" * 40


def _utc(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


def _lock():
    return LockPesos(
        PesoFijado("NeoQuasar/Kronos-small", REV, {"m": "0" * 64}),
        PesoFijado("NeoQuasar/Kronos-Tokenizer-base", "b" * 40, {"t": "1" * 64}),
        "c" * 40,
    )


# --- parámetros congelados ----------------------------------------------------------

def test_los_parametros_del_protocolo_estan_congelados():
    p = parametros_k1(_lock())
    assert (p.lookback, p.horizon, p.n_trajectories) == (400, 24, 8)
    assert (p.temperature, p.top_p, p.seed, p.usa_volumen) == (D("1.0"), D("0.9"), 20261015, False)
    assert (p.model_id, p.weights_revision, p.code_commit) == ("NeoQuasar/Kronos-small", REV, "c" * 40)
    assert (k1.PRIMERA_FECHA, k1.PASO_DIAS) == (date(2024, 7, 1), 3)


def test_el_protocolo_real_existe_y_declara_los_mismos_numeros():
    """El texto congelado y el código no pueden divergir sin que una prueba lo note."""
    ruta = k1.RUTA_PROTOCOLO
    if not ruta.exists():  # pruebas lanzadas fuera de la raíz del proyecto
        pytest.skip("docs/protocolo-k1-kronos.md no está en el directorio actual")
    texto = ruta.read_text(encoding="utf-8")
    for fragmento in ("20261015", "20261005", "5 fechas consecutivas", "cada 3 días", "8 por pronóstico", "2024-07-01"):
        assert fragmento in texto, fragmento
    assert len(huella_protocolo(ruta)) == 64


def test_la_huella_no_cambia_por_los_saltos_de_linea(tmp_path):
    a, b, c = tmp_path / "a.md", tmp_path / "b.md", tmp_path / "c.md"
    a.write_bytes(b"linea 1\nlinea 2\n")
    b.write_bytes(b"linea 1\r\nlinea 2\r\n")
    c.write_bytes(b"linea 1\nlinea 2 distinta\n")
    assert huella_protocolo(a) == huella_protocolo(b) != huella_protocolo(c)


# --- fechas -----------------------------------------------------------------------

def test_fechas_primarias_cada_tres_dias_desde_el_corte():
    f = fechas_primarias(date(2024, 7, 13))
    assert f == [date(2024, 7, 1), date(2024, 7, 4), date(2024, 7, 7), date(2024, 7, 10), date(2024, 7, 13)]
    assert fechas_primarias(date(2024, 6, 30)) == []


def test_fechas_de_diagnostico_son_todas_anteriores_al_corte():
    f = fechas_diagnostico()
    assert f[0] == date(2023, 7, 1) and all(d < k1.PRIMERA_FECHA for d in f) and len(f) == 25
    assert all((b - a).days == 15 for a, b in zip(f, f[1:]))


def _vela(abre, cerrada=True):
    return {"symbol": "X", "timeframe": "1h", "open_time": abre, "open": D(1), "high": D(1), "low": D(1),
            "close": D(1), "volume": D(1), "cerrada": cerrada}


def test_ultima_fecha_posible_es_la_del_final_mas_corto_entre_monedas():
    velas = {
        "A": [_vela(_utc(2026, 10, 14, 3))],   # termina 04:00 del 14
        "B": [_vela(_utc(2026, 10, 13, 23))],  # termina 00:00 del 14
    }
    assert ultima_fecha_posible(velas) == date(2026, 10, 14)
    velas["B"] = [_vela(_utc(2026, 10, 13, 22))]  # termina 23:00 del 13: aún no existe la medianoche del 14
    assert ultima_fecha_posible(velas) == date(2026, 10, 13)


def test_una_vela_no_cerrada_no_cuenta_para_la_ultima_fecha():
    velas = {"A": [_vela(_utc(2026, 10, 10, 5)), _vela(_utc(2026, 10, 14, 5), cerrada=False)]}
    assert ultima_fecha_posible(velas) == date(2026, 10, 10)


def test_las_fechas_se_fijan_una_vez_y_no_cambian(tmp_path):
    ruta = tmp_path / "fechas.json"
    primera = cargar_o_fijar_fechas(
        ruta, "h" * 64, lambda: {"primarias": [date(2024, 7, 1)], "diagnostico": [date(2023, 7, 1)]}, ahora=_utc(2026, 10, 15)
    )
    llamado = []
    segunda = cargar_o_fijar_fechas(ruta, "h" * 64, lambda: llamado.append(1) or {"primarias": [], "diagnostico": []}, ahora=_utc(2026, 11, 1))
    assert primera == segunda and llamado == []
    assert json.loads(ruta.read_text())["protocolo_sha256"] == "h" * 64


def test_si_el_protocolo_cambio_no_se_reanudan_las_fechas(tmp_path):
    ruta = tmp_path / "fechas.json"
    cargar_o_fijar_fechas(ruta, "h" * 64, lambda: {"primarias": [], "diagnostico": []}, ahora=_utc(2026, 10, 15))
    with pytest.raises(ValueError, match="experimento nuevo"):
        cargar_o_fijar_fechas(ruta, "otra" * 16, lambda: {"primarias": [], "diagnostico": []}, ahora=_utc(2026, 10, 16))


# --- generación reanudable ------------------------------------------------------------

def _serie(symbol, desde, n):
    return [{**_vela(desde + H * i), "symbol": symbol} for i in range(n)]


class PredictorFijo:
    def __init__(self):
        self.llamadas: list[tuple[str, datetime]] = []

    def cierres_finales(self, contextos, params):
        for c in contextos:
            self.llamadas.append((c.symbol, c.as_of))
        return [[c.ultimo_cierre * D("1.01")] * params.n_trajectories for c in contextos]


def _mundo_generacion():
    primero = _utc(2024, 7, 1)
    inicio = primero - H * 400
    return {s: _serie(s, inicio, 400 + 24 * 8) for s in ("AUSDT", "BUSDT")}


def test_genera_en_orden_fecha_simbolo_y_guarda_cada_pronostico():
    velas, pred, guardados = _mundo_generacion(), PredictorFijo(), []
    r = generar_pronosticos(
        velas, fechas=[date(2024, 7, 4), date(2024, 7, 1)], simbolos=["AUSDT", "BUSDT"], predictor=pred,
        params=parametros_k1(_lock()), ya_hechos=set(), guardar=lambda c, res, cierres: guardados.append((c.symbol, c.as_of, res, len(cierres))),
    )
    assert [(s, a) for s, a, _, _ in guardados] == [
        ("AUSDT", _utc(2024, 7, 1)), ("BUSDT", _utc(2024, 7, 1)), ("AUSDT", _utc(2024, 7, 4)), ("BUSDT", _utc(2024, 7, 4)),
    ]
    assert all(n == 8 and res.expected_return == D("0.01") for _, _, res, n in guardados)
    assert (r.generados, r.ya_existian, r.pendientes, r.detenido_por_tiempo) == (4, 0, 0, False)


def test_reanudar_se_salta_lo_ya_hecho_y_no_duplica():
    velas, params = _mundo_generacion(), parametros_k1(_lock())
    hechos = {("AUSDT", _utc(2024, 7, 1)), ("BUSDT", _utc(2024, 7, 1))}
    pred = PredictorFijo()
    r = generar_pronosticos(velas, fechas=[date(2024, 7, 1), date(2024, 7, 4)], simbolos=["AUSDT", "BUSDT"],
                            predictor=pred, params=params, ya_hechos=hechos, guardar=lambda *a: None)
    assert r.ya_existian == 2 and r.generados == 2
    assert {c[1] for c in pred.llamadas} == {_utc(2024, 7, 4)}


def test_se_detiene_por_tiempo_y_deja_pendiente_lo_que_falta():
    velas, params = _mundo_generacion(), parametros_k1(_lock())
    reloj = iter([0.0, 0.0, 1.0, 100.0, 100.0])  # inicio, 3 comprobaciones (la 3.ª pasa el límite), fin
    r = generar_pronosticos(velas, fechas=[date(2024, 7, 1), date(2024, 7, 4)], simbolos=["AUSDT", "BUSDT"],
                            predictor=PredictorFijo(), params=params, ya_hechos=set(), guardar=lambda *a: None,
                            max_segundos=50, reloj=lambda: next(reloj))
    assert r.detenido_por_tiempo and r.generados == 2 and r.pendientes == 2


def test_un_contexto_incompleto_se_cuenta_y_no_se_pronostica():
    velas = _mundo_generacion()
    velas["BUSDT"] = [v for v in velas["BUSDT"] if v["open_time"] != _utc(2024, 7, 1) - H * 50]  # hueco
    pred = PredictorFijo()
    r = generar_pronosticos(velas, fechas=[date(2024, 7, 1)], simbolos=["AUSDT", "BUSDT", "CUSDT"], predictor=pred,
                            params=parametros_k1(_lock()), ya_hechos=set(), guardar=lambda *a: None)
    assert [s for s, _ in pred.llamadas] == ["AUSDT"]
    assert sum(r.sin_contexto.values()) == 2  # BUSDT con hueco y CUSDT sin velas


def test_el_pronostico_no_ve_velas_posteriores_a_su_fecha():
    """Con el historial completo cargado, el contexto de una fecha pasada es idéntico al de la serie truncada."""
    velas = _mundo_generacion()
    visto = {}
    class Espia(PredictorFijo):
        def cierres_finales(self, contextos, params):
            visto[contextos[0].as_of] = contextos[0].velas[-1]["open_time"]
            return super().cierres_finales(contextos, params)
    generar_pronosticos(velas, fechas=[date(2024, 7, 1), date(2024, 7, 4)], simbolos=["AUSDT"], predictor=Espia(),
                        params=parametros_k1(_lock()), ya_hechos=set(), guardar=lambda *a: None)
    assert all(ultima + H <= as_of for as_of, ultima in visto.items())


# --- observaciones para evaluar --------------------------------------------------------

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
DIAS = 260


def _precio(h: float) -> Decimal:
    return Decimal(str(round(100 + h * 0.02 + 8 * math.sin(h / 60) + 3 * math.sin(h * 1.7), 6)))


def _diarias() -> list[Candle]:
    out = []
    for d in range(DIAS):
        c, o = _precio(d * 24 + 23), _precio(d * 24)
        out.append(Candle(symbol="X", timeframe="1d", open_time=T0 + timedelta(days=d), open=o,
                          high=max(o, c) * D("1.01"), low=min(o, c) * D("0.99"), close=c, volume=D(1000 + (d * 37) % 400)))
    return out


def _horarias() -> list[Candle]:
    out = []
    for h in range(DIAS * 24):
        o, c = _precio(h), _precio(h + 1)
        out.append(Candle(symbol="X", timeframe="1h", open_time=T0 + timedelta(hours=h), open=o,
                          high=max(o, c) * D("1.001"), low=min(o, c) * D("0.999"), close=c, volume=D(10)))
    return out


def _pronostico(velas, dia, retorno="0.01"):
    as_of = datetime(dia.year, dia.month, dia.day, tzinfo=timezone.utc)
    cierre = next(v["close"] for v in velas if v["open_time"] == as_of - H)
    return PronosticoGuardado("X", as_of, cierre, D(retorno))


def test_une_pronostico_desenlace_y_lineas_base_con_los_mismos_datos_del_arnes():
    diarias, horarias = _diarias(), _horarias()
    arnes = {o.dia: o for o in observaciones_de_simbolo("X", diarias, horarias)}
    dia = date(2025, 7, 1)
    assert arnes[dia].retorno_neto is not None
    obs, descartes = construir_observaciones_k1([_pronostico(horarias, dia, "0.02")], {"X": diarias}, {"X": horarias})
    (o,) = obs
    assert descartes == {}
    assert (o.symbol, o.dia, o.kronos) == ("X", dia, D("0.02"))
    assert o.retorno_neto == arnes[dia].retorno_neto and o.score_v1 == arnes[dia].score  # idéntico al arnés del score
    entrada = {v["open_time"]: v["close"] for v in horarias}
    ent = datetime(2025, 6, 30, 23, tzinfo=timezone.utc)
    assert o.mom_24h == entrada[ent] / entrada[ent - H * 24] - 1
    assert o.mom_7d == entrada[ent] / entrada[ent - H * 168] - 1


def test_descarta_y_cuenta_sin_rellenar():
    diarias, horarias = _diarias(), _horarias()
    p_sin_arnes = _pronostico(horarias, date(2025, 2, 1))          # antes del calentamiento del score
    ult = max(o.dia for o in observaciones_de_simbolo("X", diarias, horarias))
    censurada = [o for o in observaciones_de_simbolo("X", diarias, horarias) if o.retorno_neto is None]
    p_cierre_malo = PronosticoGuardado("X", datetime(2025, 7, 1, tzinfo=timezone.utc), D("1"), D("0.01"))
    pronos = [p_sin_arnes, p_cierre_malo]
    if censurada:
        pronos.append(_pronostico(horarias, censurada[0].dia))
    obs, descartes = construir_observaciones_k1(pronos, {"X": diarias}, {"X": horarias})
    assert obs == []
    assert descartes["sin_observacion_del_arnes"] == 1 and descartes["cierre_inconsistente"] == 1
    if censurada:
        assert descartes["censurada"] == 1
    assert ult >= date(2025, 7, 1)


def test_el_informe_no_presenta_un_intervalo_degenerado_como_si_fuera_preciso():
    from app.domain.rules.kronos_k1 import K1_BLOQUE_FECHAS, IntervaloBootstrap
    from app.experiments.kronos.k1_informe import _ic

    pocos = IntervaloBootstrap(K1_BLOQUE_FECHAS, D("-0.1781"), D("-0.1781"), D("-0.1781"))
    texto = _ic(pocos)
    assert "no estimable" in texto and "-0.1781 [IC95 -0.1781" not in texto and "n=5" in texto
    suficientes = IntervaloBootstrap(K1_BLOQUE_FECHAS + 1, D("0.05"), D("-0.02"), D("0.11"))
    assert _ic(suficientes) == "+0.0500 [IC95 -0.0200, +0.1100] (n=6 fechas)"
