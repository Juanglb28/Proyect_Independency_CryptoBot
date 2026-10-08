"""Estudio H1 (app.data.support_study) con datos sintéticos deterministas."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal


from app.data.barrier_study import ResultadoBarrera
from app.data.exchange_client import Candle
from app.data.support_study import (
    BASE,
    S1_COND,
    S2_COND,
    Comparacion,
    Conteo,
    IntervaloDiferencia,
    Observacion,
    Particion,
    Veredicto,
    analizar,
    contar,
    decidir,
    diferencia_con_bootstrap,
    fecha_de_corte,
    observaciones_de_simbolo,
)

INICIO = datetime(2025, 1, 1, tzinfo=timezone.utc)
HORAS = 400 * 24


def _velas_1h(horas: int = HORAS, semilla: int = 3, *, shock_desde: int | None = None) -> list[Candle]:
    rng = random.Random(semilla)
    precio = Decimal(100)
    velas: list[Candle] = []
    for h in range(horas):
        abre = precio
        precio = max(Decimal(1), precio * (Decimal(1) + Decimal(rng.randint(-8, 8)) / Decimal(2000)))
        alto = max(abre, precio) * Decimal("1.0005")
        bajo = min(abre, precio) * Decimal("0.9995")
        if shock_desde is not None and h >= shock_desde:
            alto = alto * Decimal("1.3")  # toca cualquier objetivo +12%
        velas.append(
            Candle(
                symbol="TESTUSDT", timeframe="1h", open_time=INICIO + timedelta(hours=h),
                open=abre, high=alto, low=bajo, close=precio, volume=Decimal(1000),
            )
        )
    return velas


def _agregar(velas_1h: list[Candle], horas: int, timeframe: str) -> list[Candle]:
    salida: list[Candle] = []
    for i in range(0, len(velas_1h) - horas + 1, horas):
        grupo = velas_1h[i : i + horas]
        salida.append(
            Candle(
                symbol="TESTUSDT", timeframe=timeframe, open_time=grupo[0]["open_time"],
                open=grupo[0]["open"], high=max(c["high"] for c in grupo), low=min(c["low"] for c in grupo),
                close=grupo[-1]["close"], volume=sum((c["volume"] for c in grupo), Decimal(0)),
            )
        )
    return salida


def _series(horas: int = HORAS, **kw):
    h1 = _velas_1h(horas, **kw)
    return _agregar(h1, 24, "1d"), _agregar(h1, 4, "4h"), h1


def test_genera_observaciones_solo_con_calentamiento_suficiente():
    d, c4, h1 = _series()
    obs = observaciones_de_simbolo("TESTUSDT", d, c4, h1)
    assert obs, "debería haber días elegibles"
    # Con 190 velas diarias de calentamiento, el primer día elegible no puede ser anterior al día 190.
    assert min(o.fecha for o in obs) >= (INICIO + timedelta(days=190)).date()
    assert all(BASE in o.banderas for o in obs)


def test_series_cortas_no_generan_observaciones():
    d, c4, h1 = _series(horas=100 * 24)
    assert observaciones_de_simbolo("TESTUSDT", d, c4, h1) == []


def test_ausencia_de_look_ahead_las_banderas_no_cambian_al_recortar_el_futuro():
    d, c4, h1 = _series()
    completas = {o.fecha: o for o in observaciones_de_simbolo("TESTUSDT", d, c4, h1)}
    corte = INICIO + timedelta(days=300)
    d2 = [c for c in d if c["open_time"] < corte]
    c42 = [c for c in c4 if c["open_time"] < corte]
    h12 = [c for c in h1 if c["open_time"] < corte]
    recortadas = observaciones_de_simbolo("TESTUSDT", d2, c42, h12)
    assert recortadas
    for o in recortadas:
        assert o.banderas == completas[o.fecha].banderas, o.fecha
        assert o.inicio_de_racha == completas[o.fecha].inicio_de_racha, o.fecha


def test_el_desenlace_usa_solo_velas_posteriores_a_la_decision():
    # Sin shock: la serie casi plana no toca ±8/12%; con shock desde la hora X, las decisiones previas tocan el objetivo.
    d, c4, h1 = _series(shock_desde=300 * 24)
    obs = observaciones_de_simbolo("TESTUSDT", d, c4, h1)
    antes = [o for o in obs if o.fecha < (INICIO + timedelta(days=299)).date()]
    assert antes and all(o.resultado == ResultadoBarrera.TARGET_PRIMERO for o in antes if o.resultado != ResultadoBarrera.STOP_PRIMERO)


def test_episodios_marcan_solo_el_primer_dia_de_una_racha():
    d, c4, h1 = _series()
    obs = observaciones_de_simbolo("TESTUSDT", d, c4, h1)
    por_fecha = {o.fecha: o for o in obs}
    for grupo in (S1_COND, S2_COND, BASE):
        for o in obs:
            if grupo in o.banderas:
                ayer = por_fecha.get(o.fecha - timedelta(days=1))
                continua = ayer is not None and grupo in ayer.banderas
                assert (grupo in o.inicio_de_racha) == (not continua), (grupo, o.fecha)


def _obs(fecha: date, resultado: ResultadoBarrera, grupos=(BASE,), inicio=()) -> Observacion:
    return Observacion("X", fecha, resultado, frozenset(grupos), frozenset(inicio))


T, S = ResultadoBarrera.TARGET_PRIMERO, ResultadoBarrera.STOP_PRIMERO


def test_contar_y_tasa():
    c = contar([_obs(date(2026, 1, 1), T), _obs(date(2026, 1, 2), S), _obs(date(2026, 1, 3), ResultadoBarrera.AMBIGUA)])
    assert (c.objetivo, c.stop, c.ambiguas, c.sin_resolver, c.resueltas) == (1, 1, 1, 0, 2)
    assert c.tasa == Decimal("0.5")
    assert Conteo().tasa is None


def test_bootstrap_es_determinista_y_detecta_una_diferencia_grande():
    dias = [date(2026, 1, 1) + timedelta(days=i) for i in range(120)]
    base = [_obs(d, T if i % 4 == 0 else S) for i, d in enumerate(dias)]  # tasa 25 %
    cond = [_obs(d, T if i % 10 != 0 else S, grupos=(BASE, S1_COND), inicio=(S1_COND,)) for i, d in enumerate(dias)]  # 90 %
    r1 = diferencia_con_bootstrap(cond, base)
    r2 = diferencia_con_bootstrap(cond, base)
    assert r1 == r2
    assert r1.diferencia is not None and r1.diferencia > Decimal("0.5")
    assert r1.bajo is not None and r1.bajo > 0


def test_bootstrap_sin_diferencia_cruza_cero():
    dias = [date(2026, 1, 1) + timedelta(days=i) for i in range(120)]
    base = [_obs(d, T if i % 2 == 0 else S) for i, d in enumerate(dias)]
    cond = [_obs(d, T if i % 2 == 0 else S, grupos=(BASE, S1_COND), inicio=(S1_COND,)) for i, d in enumerate(dias)]
    r = diferencia_con_bootstrap(cond, base)
    assert r.diferencia == 0
    assert r.bajo is not None and r.bajo <= 0 <= r.alto


def test_bootstrap_sin_resueltas_devuelve_nd():
    assert diferencia_con_bootstrap([], [_obs(date(2026, 1, 1), T)]) == IntervaloDiferencia(None, None, None, 0)


def test_fecha_de_corte_es_el_70_por_ciento_del_rango_de_fechas():
    obs = [_obs(date(2026, 1, 1), T), _obs(date(2026, 1, 11), T)]
    assert fecha_de_corte(obs) == date(2026, 1, 8)  # 10 días * 0,70 = 7
    assert fecha_de_corte([]) is None


def _cmp(grupo, particion, *, resueltas, bajo, tasa, diferencia=Decimal("0.1")):
    objetivo = int(resueltas * tasa)
    return Comparacion(
        grupo=grupo, particion=particion,
        episodios=Conteo(objetivo=objetivo, stop=resueltas - objetivo),
        senales=Conteo(objetivo=objetivo, stop=resueltas - objetivo),
        base=Conteo(objetivo=30, stop=70),
        intervalo=IntervaloDiferencia(diferencia, bajo, Decimal("0.3"), 2000),
        equilibrio=Decimal("0.42"),
    )


def _mundo(c1, c2, e1=Decimal("0.1"), e2=Decimal("0.2")):
    return {
        (S1_COND, Particion.CONFIRMACION): c1,
        (S2_COND, Particion.CONFIRMACION): c2,
        (S1_COND, Particion.EXPLORACION): _cmp(S1_COND, Particion.EXPLORACION, resueltas=50, bajo=Decimal("0"), tasa=0.5, diferencia=e1),
        (S2_COND, Particion.EXPLORACION): _cmp(S2_COND, Particion.EXPLORACION, resueltas=50, bajo=Decimal("0"), tasa=0.5, diferencia=e2),
    }


def test_decision_muestra_insuficiente_es_no_concluyente():
    pobre = _cmp(S1_COND, Particion.CONFIRMACION, resueltas=10, bajo=Decimal("0.2"), tasa=0.9)
    pobre2 = _cmp(S2_COND, Particion.CONFIRMACION, resueltas=29, bajo=Decimal("0.2"), tasa=0.9)
    assert decidir(_mundo(pobre, pobre2)) == (Veredicto.NO_CONCLUYENTE, None)


def test_decision_no_respaldada_si_el_intervalo_cruza_cero_o_no_supera_equilibrio():
    cruza = _cmp(S1_COND, Particion.CONFIRMACION, resueltas=60, bajo=Decimal("-0.05"), tasa=0.6)
    bajo_eq = _cmp(S2_COND, Particion.CONFIRMACION, resueltas=60, bajo=Decimal("0.05"), tasa=0.30)
    assert decidir(_mundo(cruza, bajo_eq)) == (Veredicto.NO_RESPALDADA, None)


def test_decision_respaldada_gana_la_mayor_diferencia_en_exploracion():
    ok1 = _cmp(S1_COND, Particion.CONFIRMACION, resueltas=60, bajo=Decimal("0.05"), tasa=0.6)
    ok2 = _cmp(S2_COND, Particion.CONFIRMACION, resueltas=60, bajo=Decimal("0.05"), tasa=0.6)
    assert decidir(_mundo(ok1, ok2, e1=Decimal("0.3"), e2=Decimal("0.1"))) == (Veredicto.RESPALDADA, S1_COND)
    assert decidir(_mundo(ok1, ok2, e1=Decimal("0.1"), e2=Decimal("0.3"))) == (Veredicto.RESPALDADA, S2_COND)


def test_la_confirmacion_decide_aunque_la_exploracion_sea_buena():
    mal = _cmp(S1_COND, Particion.CONFIRMACION, resueltas=60, bajo=Decimal("-0.1"), tasa=0.5)
    ok = _cmp(S2_COND, Particion.CONFIRMACION, resueltas=60, bajo=Decimal("0.05"), tasa=0.6)
    assert decidir(_mundo(mal, ok, e1=Decimal("0.9"), e2=Decimal("0.01"))) == (Veredicto.RESPALDADA, S2_COND)


def test_analizar_de_extremo_a_extremo_no_falla_con_datos_sinteticos():
    d, c4, h1 = _series()
    informe = analizar(observaciones_de_simbolo("TESTUSDT", d, c4, h1))
    assert informe is not None
    assert informe.veredicto in set(Veredicto)
    assert (S1_COND, Particion.CONFIRMACION) in informe.comparaciones
    assert analizar([]) is None
