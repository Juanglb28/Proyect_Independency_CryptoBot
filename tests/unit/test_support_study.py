"""Estudio H1 (app.data.support_study) con datos sintéticos deterministas."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.data.barrier_study import ResultadoBarrera
from app.data.exchange_client import Candle
from app.data.support_study import (
    BASE,
    ITERACIONES_BOOTSTRAP,
    RETORNO,
    S1_COND,
    S2_COND,
    SEMILLA_BOOTSTRAP,
    TASA,
    Comparacion,
    Conteo,
    EstadoDefinicion,
    Estimacion,
    Observacion,
    Particion,
    Veredicto,
    analizar,
    bootstrap_grupos,
    contar,
    decidir,
    fecha_de_corte,
    observaciones_de_simbolo,
    seleccionar,
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


def _obs(
    fecha: date, resultado: ResultadoBarrera, grupos=(BASE,), inicio=(), *, symbol="X",
    resuelta_en=None, retorno=None, cierre=None,
) -> Observacion:
    return Observacion(
        symbol, fecha, resultado, frozenset(grupos), frozenset(inicio),
        resuelta_en=resuelta_en, retorno_neto=retorno, cierre_simulado_en=cierre,
    )


T, S = ResultadoBarrera.TARGET_PRIMERO, ResultadoBarrera.STOP_PRIMERO


def _dias(n: int, desde: date = date(2026, 1, 1)) -> list[date]:
    return [desde + timedelta(days=i) for i in range(n)]


def test_contar_y_tasa():
    c = contar([_obs(date(2026, 1, 1), T), _obs(date(2026, 1, 2), S), _obs(date(2026, 1, 3), ResultadoBarrera.AMBIGUA)])
    assert (c.objetivo, c.stop, c.ambiguas, c.sin_resolver, c.resueltas) == (1, 1, 1, 0, 2)
    assert c.tasa == Decimal("0.5")
    assert Conteo().tasa is None


def test_fecha_de_corte_es_el_70_por_ciento_del_rango_de_fechas():
    obs = [_obs(date(2026, 1, 1), T), _obs(date(2026, 1, 11), T)]
    assert fecha_de_corte(obs) == date(2026, 1, 8)  # 10 días * 0,70 = 7
    assert fecha_de_corte([]) is None


# --- Purga en exploración ---------------------------------------------------

CORTE = date(2026, 2, 1)
MEDIANOCHE_CORTE = datetime(2026, 2, 1, tzinfo=timezone.utc)


def test_purga_excluye_de_exploracion_los_desenlaces_en_o_despues_del_corte():
    antes = _obs(date(2026, 1, 30), T, resuelta_en=MEDIANOCHE_CORTE - timedelta(hours=1))
    justo = _obs(date(2026, 1, 30), T, resuelta_en=MEDIANOCHE_CORTE)
    despues = _obs(date(2026, 1, 31), S, resuelta_en=MEDIANOCHE_CORTE + timedelta(days=3))
    sin_resolver = _obs(date(2026, 1, 31), ResultadoBarrera.SIN_RESOLVER, resuelta_en=None)
    elegidas, purgadas = seleccionar([antes, justo, despues, sin_resolver], BASE, Particion.EXPLORACION, CORTE, solo_episodios=False)
    assert elegidas == [antes]
    assert purgadas == 3


def test_confirmacion_no_se_purga():
    sin_resolver = _obs(date(2026, 2, 5), ResultadoBarrera.SIN_RESOLVER, resuelta_en=None)
    tardia = _obs(date(2026, 2, 6), T, resuelta_en=datetime(2026, 6, 1, tzinfo=timezone.utc))
    elegidas, purgadas = seleccionar([sin_resolver, tardia], BASE, Particion.CONFIRMACION, CORTE, solo_episodios=False)
    assert elegidas == [sin_resolver, tardia] and purgadas == 0


def test_purga_por_simulacion_usa_el_cierre_total_simulado():
    o = _obs(
        date(2026, 1, 30), T,
        resuelta_en=MEDIANOCHE_CORTE - timedelta(hours=1),        # la barrera se toca antes del corte
        cierre=MEDIANOCHE_CORTE + timedelta(days=2),              # pero la salida real cierra después
    )
    assert seleccionar([o], BASE, Particion.EXPLORACION, CORTE, solo_episodios=False)[1] == 0
    elegidas, purgadas = seleccionar([o], BASE, Particion.EXPLORACION, CORTE, solo_episodios=False, purgar_por="simulacion")
    assert elegidas == [] and purgadas == 1


def test_seleccionar_solo_episodios_filtra_por_inicio_de_racha():
    a = _obs(date(2026, 1, 10), T, grupos=(BASE, S1_COND), inicio=(S1_COND,), resuelta_en=MEDIANOCHE_CORTE - timedelta(days=5))
    b = _obs(date(2026, 1, 11), T, grupos=(BASE, S1_COND), resuelta_en=MEDIANOCHE_CORTE - timedelta(days=5))
    assert seleccionar([a, b], S1_COND, Particion.EXPLORACION, CORTE, solo_episodios=True)[0] == [a]
    assert seleccionar([a, b], S1_COND, Particion.EXPLORACION, CORTE, solo_episodios=False)[0] == [a, b]


# --- Bootstrap y línea base ajustada ----------------------------------------


def test_bootstrap_es_determinista_y_detecta_una_diferencia_grande():
    dias = _dias(120)
    base = [_obs(d, T if i % 4 == 0 else S) for i, d in enumerate(dias)]  # tasa 25 %
    cond = [_obs(d, T if i % 10 != 0 else S, grupos=(BASE, S1_COND), inicio=(S1_COND,)) for i, d in enumerate(dias)]  # 90 %
    r1 = bootstrap_grupos(base, {S1_COND: cond}, TASA)
    r2 = bootstrap_grupos(base, {S1_COND: cond}, TASA)
    assert r1 == r2
    bruta, ajustada = r1[S1_COND]
    assert bruta.diferencia is not None and bruta.diferencia > Decimal("0.5")
    assert bruta.lim_inferior_positivo and ajustada.lim_inferior_positivo
    assert bruta.remuestreos_validos == ITERACIONES_BOOTSTRAP
    assert ajustada.estratos is not None and ajustada.estratos >= 4


def test_bootstrap_cambiar_la_semilla_cambia_el_intervalo_pero_no_el_punto():
    dias = _dias(120)
    base = [_obs(d, T if i % 4 == 0 else S) for i, d in enumerate(dias)]
    cond = [_obs(d, T if i % 10 != 0 else S, grupos=(BASE, S1_COND), inicio=(S1_COND,)) for i, d in enumerate(dias)]
    a = bootstrap_grupos(base, {S1_COND: cond}, TASA, semilla=SEMILLA_BOOTSTRAP)[S1_COND][0]
    b = bootstrap_grupos(base, {S1_COND: cond}, TASA, semilla=1)[S1_COND][0]
    assert a.diferencia == b.diferencia
    assert (a.bajo, a.alto) != (b.bajo, b.alto)


def test_bootstrap_sin_diferencia_cruza_cero():
    dias = _dias(120)
    base = [_obs(d, T if i % 2 == 0 else S) for i, d in enumerate(dias)]
    cond = [_obs(d, T if i % 2 == 0 else S, grupos=(BASE, S1_COND), inicio=(S1_COND,)) for i, d in enumerate(dias)]
    bruta, _ = bootstrap_grupos(base, {S1_COND: cond}, TASA)[S1_COND]
    assert bruta.diferencia == 0
    assert bruta.bajo is not None and bruta.bajo <= 0 <= bruta.alto


def test_bootstrap_sin_resueltas_devuelve_nd():
    bruta, ajustada = bootstrap_grupos([_obs(date(2026, 1, 1), T)], {S1_COND: []}, TASA)[S1_COND]
    assert bruta.diferencia is None and ajustada.diferencia is None


def test_ajuste_pasa_en_bruto_pero_no_contra_la_base_ajustada_por_mes():
    """La señal solo aparece en meses buenos (80 % de objetivo). Frente a TODA la
    base parece mejor (+30 pp), pero frente a la base de esos mismos meses no hay
    diferencia: un filtro que solo marca días de mercado alcista no sirve."""
    base, cond = [], []
    for desde, bueno in ((date(2026, 1, 1), False), (date(2026, 2, 1), True), (date(2026, 3, 1), False), (date(2026, 4, 1), True)):
        for i, d in enumerate(_dias(28, desde)):
            gana = (i % 5 != 0) if bueno else (i % 5 == 0)
            base.append(_obs(d, T if gana else S))
            if bueno:
                cond.append(_obs(d, T if gana else S, grupos=(BASE, S1_COND), inicio=(S1_COND,)))
    bruta, ajustada = bootstrap_grupos(base, {S1_COND: cond}, TASA)[S1_COND]
    assert bruta.diferencia is not None and bruta.diferencia > Decimal("0.25")
    assert bruta.lim_inferior_positivo
    assert ajustada.diferencia == 0
    assert not ajustada.lim_inferior_positivo
    assert ajustada.estratos == 2


def test_el_ajuste_descarta_estratos_sin_datos_de_base():
    base = [_obs(d, T if i % 2 else S, symbol="Y") for i, d in enumerate(_dias(40))]
    cond = [_obs(d, T, grupos=(BASE, S1_COND), inicio=(S1_COND,), symbol="X") for d in _dias(40)]
    bruta, ajustada = bootstrap_grupos(base, {S1_COND: cond}, TASA)[S1_COND]
    assert bruta.diferencia is not None          # la comparación bruta sí existe
    assert ajustada.diferencia is None           # la moneda X no tiene línea base propia -> sin ajuste


def test_la_metrica_de_retorno_ignora_las_posiciones_sin_cerrar():
    dias = _dias(40)
    base = [_obs(d, T, retorno=Decimal("0.01")) for d in dias]
    cond = [
        _obs(d, T, grupos=(BASE, S1_COND), inicio=(S1_COND,), retorno=Decimal("0.05") if i % 2 == 0 else None)
        for i, d in enumerate(dias)
    ]
    bruta, _ = bootstrap_grupos(base, {S1_COND: cond}, RETORNO)[S1_COND]
    assert abs(bruta.cond - Decimal("0.05")) < Decimal("1e-12")
    assert abs(bruta.base - Decimal("0.01")) < Decimal("1e-12")


# --- Veredicto por definición ------------------------------------------------


def _est(diferencia, bajo) -> Estimacion:
    return Estimacion(Decimal("0.6"), Decimal("0.4"), diferencia, bajo, Decimal("0.4"), 2000, 5)


def _cmp(grupo, particion, *, resueltas, tasa=0.6, bajo_bruta=Decimal("0.05"), bajo_ajust=Decimal("0.05"),
         dif_bruta=Decimal("0.1"), dif_ajust=Decimal("0.1")):
    objetivo = int(resueltas * tasa)
    vacia = Estimacion(None, None, None, None, None, 0)
    return Comparacion(
        grupo=grupo, particion=particion,
        episodios=Conteo(objetivo=objetivo, stop=resueltas - objetivo),
        senales=Conteo(objetivo=objetivo, stop=resueltas - objetivo),
        base=Conteo(objetivo=30, stop=70),
        purgadas_episodios=0, purgadas_base=0,
        tasa_bruta=_est(dif_bruta, bajo_bruta), tasa_ajustada=_est(dif_ajust, bajo_ajust),
        retorno_bruto=vacia, retorno_ajustado=vacia,
        equilibrio=Decimal("0.42"),
    )


def _mundo(c1, c2, expl1=None, expl2=None):
    expl1 = expl1 or _cmp(S1_COND, Particion.EXPLORACION, resueltas=50)
    expl2 = expl2 or _cmp(S2_COND, Particion.EXPLORACION, resueltas=50)
    return {
        (S1_COND, Particion.CONFIRMACION): c1,
        (S2_COND, Particion.CONFIRMACION): c2,
        (S1_COND, Particion.EXPLORACION): expl1,
        (S2_COND, Particion.EXPLORACION): expl2,
    }


def test_ninguna_definicion_evaluable_es_no_concluyente():
    p1 = _cmp(S1_COND, Particion.CONFIRMACION, resueltas=10)
    p2 = _cmp(S2_COND, Particion.CONFIRMACION, resueltas=29)
    v, g, e = decidir(_mundo(p1, p2))
    assert (v, g) == (Veredicto.NO_CONCLUYENTE, None)
    assert set(e.values()) == {EstadoDefinicion.SIN_EVALUAR}


def test_una_falla_y_la_otra_sin_muestra_no_es_no_respaldada_a_secas():
    falla = _cmp(S1_COND, Particion.CONFIRMACION, resueltas=60, bajo_bruta=Decimal("-0.05"))
    pobre = _cmp(S2_COND, Particion.CONFIRMACION, resueltas=12)
    v, g, e = decidir(_mundo(falla, pobre))
    assert (v, g) == (Veredicto.NO_RESPALDADA_PARCIAL, None)
    assert e[S1_COND] == EstadoDefinicion.NO_PASA and e[S2_COND] == EstadoDefinicion.SIN_EVALUAR


def test_ambas_evaluadas_y_fallan_es_no_respaldada():
    cruza = _cmp(S1_COND, Particion.CONFIRMACION, resueltas=60, bajo_bruta=Decimal("-0.05"))
    bajo_eq = _cmp(S2_COND, Particion.CONFIRMACION, resueltas=60, tasa=0.30)
    v, g, e = decidir(_mundo(cruza, bajo_eq))
    assert (v, g) == (Veredicto.NO_RESPALDADA, None)
    assert set(e.values()) == {EstadoDefinicion.NO_PASA}


def test_pasar_solo_contra_la_base_bruta_no_basta():
    solo_bruta = _cmp(S1_COND, Particion.CONFIRMACION, resueltas=60, bajo_bruta=Decimal("0.05"), bajo_ajust=Decimal("0"))
    otra = _cmp(S2_COND, Particion.CONFIRMACION, resueltas=60, bajo_ajust=Decimal("-0.02"))
    v, _, e = decidir(_mundo(solo_bruta, otra))
    assert v == Veredicto.NO_RESPALDADA
    assert e[S1_COND] == EstadoDefinicion.NO_PASA


def test_una_pasa_y_la_otra_sin_muestra_es_respaldada():
    ok = _cmp(S1_COND, Particion.CONFIRMACION, resueltas=60)
    pobre = _cmp(S2_COND, Particion.CONFIRMACION, resueltas=12)
    v, g, e = decidir(_mundo(ok, pobre))
    assert (v, g) == (Veredicto.RESPALDADA, S1_COND)
    assert e[S2_COND] == EstadoDefinicion.SIN_EVALUAR


def test_si_pasan_ambas_gana_la_mayor_diferencia_minima_en_exploracion():
    ok1 = _cmp(S1_COND, Particion.CONFIRMACION, resueltas=60)
    ok2 = _cmp(S2_COND, Particion.CONFIRMACION, resueltas=60)
    # S1: bruta +30 pp pero ajustada +5 pp (mín 5); S2: +20 / +15 (mín 15) -> gana S2.
    e1 = _cmp(S1_COND, Particion.EXPLORACION, resueltas=50, dif_bruta=Decimal("0.30"), dif_ajust=Decimal("0.05"))
    e2 = _cmp(S2_COND, Particion.EXPLORACION, resueltas=50, dif_bruta=Decimal("0.20"), dif_ajust=Decimal("0.15"))
    assert decidir(_mundo(ok1, ok2, e1, e2))[:2] == (Veredicto.RESPALDADA, S2_COND)


def test_la_confirmacion_decide_aunque_la_exploracion_sea_buena():
    mal = _cmp(S1_COND, Particion.CONFIRMACION, resueltas=60, bajo_bruta=Decimal("-0.1"))
    ok = _cmp(S2_COND, Particion.CONFIRMACION, resueltas=60)
    buena = _cmp(S1_COND, Particion.EXPLORACION, resueltas=50, dif_bruta=Decimal("0.9"), dif_ajust=Decimal("0.9"))
    pobre = _cmp(S2_COND, Particion.EXPLORACION, resueltas=50, dif_bruta=Decimal("0.01"), dif_ajust=Decimal("0.01"))
    assert decidir(_mundo(mal, ok, buena, pobre))[:2] == (Veredicto.RESPALDADA, S2_COND)


# --- Extremo a extremo y protocolo -------------------------------------------


def test_analizar_de_extremo_a_extremo_no_falla_con_datos_sinteticos():
    d, c4, h1 = _series()
    informe = analizar(observaciones_de_simbolo("TESTUSDT", d, c4, h1))
    assert informe is not None
    assert informe.veredicto in set(Veredicto)
    assert set(informe.estados) == {S1_COND, S2_COND}
    c = informe.comparaciones[(S1_COND, Particion.CONFIRMACION)]
    assert c.purgadas_episodios == 0  # la confirmación nunca se purga
    assert analizar([]) is None


def test_analizar_es_reproducible():
    d, c4, h1 = _series()
    obs = observaciones_de_simbolo("TESTUSDT", d, c4, h1)
    a, b = analizar(obs), analizar(obs)
    assert a is not None and a == b


def test_el_protocolo_esta_en_el_repo_y_recoge_los_parametros_fijados():
    from app.domain.rules import support

    texto = (Path(__file__).resolve().parents[2] / "docs" / "protocolo-h1-soporte.md").read_text(encoding="utf-8")
    assert "v2" in texto
    assert str(SEMILLA_BOOTSTRAP) in texto
    assert str(ITERACIONES_BOOTSTRAP) in texto
    for valor in list(Veredicto) + list(EstadoDefinicion):
        assert valor.value in texto, valor
    for constante in (support.FRACTAL_N, support.VENTANA_FRACTAL_VELAS, support.VENTANA_MINIMO_VELAS,
                      support.VELAS_DIARIAS_MINIMAS, support.VELAS_4H_MINIMAS):
        assert str(constante) in texto, constante
    assert "35" in texto and "2 %" in texto
