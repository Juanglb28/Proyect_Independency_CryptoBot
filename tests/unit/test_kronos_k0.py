"""Núcleo de K0 (enmienda E6): muestreo determinista, informe y límites. Sin torch, sin red, sin base."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.rules.kronos_signal import CORTE_PREENTRENAMIENTO, ParametrosKronos
from app.experiments.kronos.k0 import (
    ejecutar_k0,
    formatear_reporte,
    instantes_de_muestra,
    retorno_realizado,
)
from app.experiments.kronos.contexto import construir_contexto
from app.experiments.kronos.pesos import LockPesos, PesoFijado

D = Decimal
H = timedelta(hours=1)
REV = "a" * 40


def _utc(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def _vela(symbol, abre, close="100", cerrada=True):
    return {
        "symbol": symbol, "timeframe": "1h", "open_time": abre, "open": D(close), "high": D(close),
        "low": D(close), "close": D(close), "volume": D("5"), "cerrada": cerrada,
    }


def _serie(symbol, desde, n, close=lambda i: "100"):
    return [_vela(symbol, desde + H * i, close(i)) for i in range(n)]


def _params(**kw):
    base = dict(
        model_id="NeoQuasar/Kronos-small", tokenizer_id="NeoQuasar/Kronos-Tokenizer-base",
        weights_revision=REV, tokenizer_revision=REV, code_commit="b" * 40,
        lookback=400, horizon=24, n_trajectories=4, temperature=D("1.0"), top_p=D("0.9"), seed=1,
    )
    base.update(kw)
    return ParametrosKronos(**base)


class PredictorFijo:
    """Devuelve siempre las mismas trayectorias: cierre final = último cierre * factor."""

    def __init__(self, factor="1.10", deterministico=True):
        self.factor = D(factor)
        self.deterministico = deterministico
        self.llamadas = 0
        self.llamadas_con: list[list[str]] = []

    def cierres_finales(self, contextos, params):
        self.llamadas += 1
        self.llamadas_con.append([c.symbol for c in contextos])
        extra = D(0) if self.deterministico else D(self.llamadas)
        return [[c.ultimo_cierre * self.factor + extra] * params.n_trajectories for c in contextos]


# --- muestreo de instantes -------------------------------------------------

def test_los_instantes_son_deterministas_y_estan_en_el_rango():
    a = instantes_de_muestra(date(2024, 7, 1), date(2024, 7, 31), 5)
    b = instantes_de_muestra(date(2024, 7, 1), date(2024, 7, 31), 5)
    assert a == b and len(a) == 5
    assert a[0] == _utc(2024, 7, 1) and a[-1] == _utc(2024, 7, 31)
    assert all(x.tzinfo is not None and x.hour == 0 for x in a)
    assert a == sorted(a)


def test_si_se_piden_mas_instantes_que_dias_se_usan_todos_los_dias_sin_repetir():
    assert len(instantes_de_muestra(date(2025, 1, 1), date(2025, 1, 3), 10)) == 3


def test_un_solo_instante_es_el_primer_dia():
    assert instantes_de_muestra(date(2025, 1, 1), date(2025, 1, 9), 1) == [_utc(2025, 1, 1)]


@pytest.mark.parametrize("desde,hasta,n", [(date(2025, 1, 2), date(2025, 1, 1), 3), (date(2025, 1, 1), date(2025, 1, 5), 0)])
def test_rangos_invalidos_se_rechazan(desde, hasta, n):
    with pytest.raises(ValueError):
        instantes_de_muestra(desde, hasta, n)


# --- retorno realizado -----------------------------------------------------

def test_retorno_realizado_usa_el_cierre_de_la_vela_final_del_horizonte():
    as_of = _utc(2025, 3, 1)
    velas = _serie("BTCUSDT", as_of - H * 400, 424, close=lambda i: "100" if i < 400 else "110")
    ctx = construir_contexto("BTCUSDT", velas, as_of=as_of, lookback=400, horizon=24)
    assert retorno_realizado(velas, ctx) == D("0.1")


def test_si_faltan_las_velas_del_horizonte_no_hay_retorno_no_se_estima():
    as_of = _utc(2025, 3, 1)
    velas = _serie("BTCUSDT", as_of - H * 400, 410)  # solo 10 velas posteriores
    ctx = construir_contexto("BTCUSDT", velas, as_of=as_of, lookback=400, horizon=24)
    assert retorno_realizado(velas, ctx) is None


# --- ejecución completa con predictor falso ---------------------------------

def _mundo(n_simbolos=2, instantes=(_utc(2025, 3, 1), _utc(2025, 3, 2))):
    inicio = instantes[0] - H * 400
    n = 400 + 24 * (len(instantes) + 1) + 24
    return {f"S{i}USDT": _serie(f"S{i}USDT", inicio, n) for i in range(n_simbolos)}, list(instantes)


def test_ejecuta_y_reporta_conteos_tiempos_y_reproducibilidad():
    velas, instantes = _mundo()
    pred = PredictorFijo()
    relojes = iter([10.0, 11.0, 12.0, 13.0, 14.0, 14.0])  # inicio, 4 avances de progreso, fin
    avances = []
    r = ejecutar_k0(
        velas, predictor=pred, params=_params(), instantes=instantes, dispositivo="cpu",
        reloj=lambda: next(relojes), progreso=lambda h, t, s: avances.append((h, t)),
    )
    assert avances == [(1, 4), (2, 4), (3, 4), (4, 4)]
    assert r.contextos_pedidos == 4 and r.contextos_utilizables == 4
    assert r.segundos_totales == 4.0 and r.segundos_por_pronostico == 1.0
    assert r.reproducible is True
    assert all(len(x) == 1 for x in pred.llamadas_con[:4])  # un contexto por llamada
    assert pred.llamadas_con[4] == ["S1USDT", "S0USDT"]  # control: otro orden y otra composición
    assert len(r.pronosticos) == 4
    assert all(p.resumen.expected_return == D("0.1") for p in r.pronosticos)


def test_un_predictor_no_determinista_se_marca_como_no_reproducible():
    velas, instantes = _mundo(1, (_utc(2025, 3, 1),))
    r = ejecutar_k0(velas, predictor=PredictorFijo(deterministico=False), params=_params(), instantes=instantes, dispositivo="cpu")
    assert r.reproducible is False
    assert "DIFERENTE" in "\n".join(formatear_reporte(r))


def test_se_puede_omitir_la_comprobacion_de_reproducibilidad():
    velas, instantes = _mundo(1, (_utc(2025, 3, 1),))
    pred = PredictorFijo()
    r = ejecutar_k0(velas, predictor=pred, params=_params(), instantes=instantes, dispositivo="cpu", comprobar_reproducibilidad=False)
    assert r.reproducible is None and pred.llamadas == 1


def test_los_contextos_incompletos_se_descartan_y_se_cuentan_sin_rellenar():
    velas, instantes = _mundo(2, (_utc(2025, 3, 1),))
    velas["S1USDT"] = [v for v in velas["S1USDT"] if v["open_time"] != _utc(2025, 3, 1) - H * 50]
    r = ejecutar_k0(velas, predictor=PredictorFijo(), params=_params(), instantes=instantes, dispositivo="cpu")
    assert r.contextos_pedidos == 2 and r.contextos_utilizables == 1
    assert r.motivos_sin_contexto == {"hueco_en_las_velas": 1} or sum(r.motivos_sin_contexto.values()) == 1


def test_sin_retorno_realizado_no_entra_a_las_comparaciones():
    as_of = _utc(2025, 3, 1)
    velas = {"BTCUSDT": _serie("BTCUSDT", as_of - H * 400, 405)}  # casi sin futuro
    r = ejecutar_k0(velas, predictor=PredictorFijo(), params=_params(), instantes=[as_of], dispositivo="cpu")
    assert r.contextos_utilizables == 1 and r.sin_retorno_realizado == 1
    assert r.sanidad is None and r.rank_ic is None


def test_los_instantes_anteriores_al_corte_se_cuentan_y_se_avisan():
    antes = CORTE_PREENTRENAMIENTO - timedelta(days=30)
    velas = {"BTCUSDT": _serie("BTCUSDT", antes - H * 400, 460)}
    r = ejecutar_k0(velas, predictor=PredictorFijo(), params=_params(), instantes=[antes], dispositivo="cpu")
    assert r.anteriores_al_corte == 1
    assert any("AVISO" in linea and "NO miden predicción" in linea for linea in formatear_reporte(r))


def test_el_informe_declara_sus_limitaciones_y_no_afirma_rendimiento():
    velas, instantes = _mundo()
    texto = "\n".join(formatear_reporte(ejecutar_k0(velas, predictor=PredictorFijo(), params=_params(), instantes=instantes, dispositivo="cpu")))
    assert "LIMITACIONES" in texto and "no prueban ni descartan" in texto
    assert "solo OHLC" in texto


def test_parametros_invalidos_se_rechazan_antes_de_predecir():
    velas, instantes = _mundo()
    pred = PredictorFijo()
    with pytest.raises(ValueError):
        ejecutar_k0(velas, predictor=pred, params=_params(lookback=500), instantes=instantes, dispositivo="cpu")
    assert pred.llamadas == 0


def test_un_predictor_que_devuelve_menos_resultados_que_contextos_falla():
    class Malo:
        def cierres_finales(self, contextos, params):
            return []

    velas, instantes = _mundo(1, (_utc(2025, 3, 1),))
    with pytest.raises(RuntimeError):
        ejecutar_k0(velas, predictor=Malo(), params=_params(), instantes=instantes, dispositivo="cpu")


# --- script ----------------------------------------------------------------

def _args(**kw):
    from scripts.kronos_k0 import parse_args

    args = parse_args([])
    for k, v in kw.items():
        setattr(args, k, v)
    return args


def _lock():
    return LockPesos(
        modelo=PesoFijado("NeoQuasar/Kronos-small", REV, {"model.safetensors": "0" * 64}),
        tokenizador=PesoFijado("NeoQuasar/Kronos-Tokenizer-base", REV, {"model.safetensors": "1" * 64}),
        code_commit="b" * 40,
    )


def test_el_script_rechaza_fechas_anteriores_al_corte_salvo_diagnostico():
    from scripts.kronos_k0 import ejecutar

    with pytest.raises(SystemExit):
        ejecutar(_args(desde=date(2024, 1, 1)), predictor=PredictorFijo(), velas_1h={}, lock=_lock(), ahora=_utc(2026, 1, 1))


def test_el_script_solo_acepta_simbolos_de_la_lista_autorizada():
    from scripts.kronos_k0 import validar_simbolos
    from app.domain.symbols import ALLOWED_SYMBOLS

    assert validar_simbolos(None) == list(ALLOWED_SYMBOLS)
    assert validar_simbolos([next(iter(ALLOWED_SYMBOLS)).lower()]) == [next(iter(ALLOWED_SYMBOLS))]
    with pytest.raises(SystemExit):
        validar_simbolos(["FAKEUSDT"])


def test_el_script_ejecuta_con_predictor_inyectado_y_el_json_es_serializable():
    from scripts.kronos_k0 import ejecutar, reporte_a_json

    desde = date(2025, 3, 1)
    velas = {"BTCUSDT": _serie("BTCUSDT", _utc(2025, 3, 1) - H * 400, 400 + 24 * 6)}
    args = _args(desde=desde, hasta=date(2025, 3, 3), fechas=2, trayectorias=4)
    r = ejecutar(args, predictor=PredictorFijo(), velas_1h=velas, lock=_lock(), ahora=_utc(2026, 1, 1))
    assert r.contextos_utilizables == 2
    datos = json.loads(json.dumps(reporte_a_json(r)))
    assert datos["parametros"]["weights_revision"] == REV
    assert datos["parametros"]["temperature"] == "1.0"
    assert len(datos["pronosticos"]) == 2


def test_los_parametros_del_script_vienen_del_bloqueo_no_de_una_rama_movil():
    from scripts.kronos_k0 import parametros_desde

    p = parametros_desde(_args(), _lock())
    assert p.weights_revision == REV and p.code_commit == "b" * 40
