"""D1 — distancia a la media de 200 periodos (4 h): reglas puras del dominio.

Fijan los parámetros del protocolo, la agregación 1 h → 4 h, la SMA/EMA (la SMA contra
`indicator_calculator.sma`), el percentil móvil, la AUSENCIA DE INFORMACIÓN FUTURA, los
episodios, la parte A, el bootstrap por bloques de días y el veredicto. Sin base, red ni IA.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.data.indicator_calculator import sma as sma_de_la_plataforma
from app.domain.rules import distancia_ma as d
from app.domain.rules.distancia_ma import (
    CALENTADA_PERCENTIL,
    CUATRO_HORAS,
    Grupo,
    ObsClasificada,
    ObsDistancia,
    Vela1h,
    Vela4h,
    VeredictoD1,
)
from app.domain.rules.kronos_k1 import IntervaloBootstrap

T0 = datetime(2022, 1, 1, tzinfo=timezone.utc)
UNA_HORA = timedelta(hours=1)


def _serie_4h(n: int, *, semilla: int = 1, saltos: tuple[int, ...] = ()) -> list[Vela4h]:
    """Paseo aleatorio determinista. `saltos` = posiciones tras las cuales falta una vela."""
    rng = random.Random(semilla)
    precio = Decimal(30000)
    t = T0
    velas = []
    for i in range(n):
        precio = precio * (1 + Decimal(rng.randint(-150, 160)) / Decimal(10000))
        o = precio * Decimal("0.999")
        velas.append(Vela4h(t, o, precio * Decimal("1.004"), precio * Decimal("0.996"), precio))
        t += CUATRO_HORAS * (2 if i in saltos else 1)
    return velas


def _obs(dia: date, dist: Decimal) -> ObsDistancia:
    return ObsDistancia(datetime(dia.year, dia.month, dia.day, tzinfo=timezone.utc), Decimal(1), dist, dist, 600)


class TestParametrosDelProtocolo:
    def test_los_valores_congelados(self):
        assert d.PERIODO_MA == 200 and d.POSICION_MINIMA == 600 and d.CALENTADA_PERCENTIL == 1080
        assert d.HORIZONTES == (6, 18, 42) and d.HORIZONTE_PRIMARIO == 18
        assert d.COLA_PCT == 5 and d.MIN_EPISODIOS == 10 and d.VELAS_FUERA_NUEVO_EPISODIO == 6
        assert d.BLOQUE_DIAS == 14 and d.ITERACIONES == 1000 and d.SEMILLA == 20261005
        assert d.UMBRAL_ALTO == Decimal("0.19") and d.UMBRAL_BAJO == Decimal("-0.25") and d.VENTANA_DIAS == 365
        assert d.ALFA_EMA == Decimal(2) / Decimal(201)


class TestAgregacion:
    def _horas(self, inicio: datetime, n: int, base: int = 100) -> list[Vela1h]:
        return [
            Vela1h(inicio + UNA_HORA * k, Decimal(base + k), Decimal(base + k + 5), Decimal(base + k - 5), Decimal(base + k + 1))
            for k in range(n)
        ]

    def test_cuatro_horas_se_agregan_con_ohlc_correcto(self):
        velas, conteo = d.agregar_a_4h(self._horas(T0, 8))
        assert len(velas) == 2 and conteo.bloques_completos == 2 and conteo.bloques_incompletos == 0
        a = velas[0]
        assert a.open_time == T0 and a.open == 100 and a.close == 104 and a.high == 108 and a.low == 95
        assert velas[1].open_time == T0 + CUATRO_HORAS

    def test_un_bloque_incompleto_no_existe_y_se_cuenta(self):
        horas = self._horas(T0, 8)
        del horas[5]  # falta la hora 05:00 del segundo bloque
        velas, conteo = d.agregar_a_4h(horas)
        assert [v.open_time for v in velas] == [T0] and conteo.bloques_incompletos == 1

    def test_los_bloques_son_utc_00_04_08(self):
        inicio = T0 + timedelta(hours=2)  # 02:00: el primer bloque queda incompleto
        velas, conteo = d.agregar_a_4h(self._horas(inicio, 6))
        assert [v.open_time for v in velas] == [T0 + CUATRO_HORAS] and conteo.bloques_incompletos == 1

    def test_entradas_invalidas_fallan_en_voz_alta(self):
        with pytest.raises(ValueError):
            d.agregar_a_4h(self._horas(T0, 2) + self._horas(T0, 1))  # duplicada
        with pytest.raises(ValueError):
            d.agregar_a_4h([Vela1h(datetime(2022, 1, 1), Decimal(1), Decimal(1), Decimal(1), Decimal(1))])  # sin zona
        with pytest.raises(ValueError):
            d.agregar_a_4h([Vela1h(T0 + timedelta(minutes=30), Decimal(1), Decimal(1), Decimal(1), Decimal(1))])


class TestControlDeAgregacion:
    def test_coincidencia_exacta(self):
        agregadas = _serie_4h(5)
        control = d.comparar_con_4h_guardadas(agregadas, agregadas[1:4])
        assert control.comparadas == 3 and control.coinciden == 3 and control.valido

    def test_una_diferencia_invalida_el_control(self):
        agregadas = _serie_4h(5)
        guardadas = [agregadas[0], Vela4h(agregadas[1].open_time, agregadas[1].open, agregadas[1].high, agregadas[1].low, agregadas[1].close + 1)]
        control = d.comparar_con_4h_guardadas(agregadas, guardadas)
        assert not control.valido and control.n_discrepancias == 1 and control.discrepancias[0][1] == "close"

    def test_velas_guardadas_sin_equivalente_se_cuentan_aparte(self):
        agregadas = _serie_4h(3)
        extra = Vela4h(T0 + CUATRO_HORAS * 50, Decimal(1), Decimal(1), Decimal(1), Decimal(1))
        control = d.comparar_con_4h_guardadas(agregadas, [*agregadas, extra])
        assert control.solo_guardadas == 1 and control.valido


class TestMediaYDistancia:
    def test_tramos_se_parten_en_los_huecos(self):
        tramos = d.tramos_contiguos(_serie_4h(10, saltos=(3,)))
        assert [len(t) for t in tramos] == [4, 6]

    def test_serie_desordenada_o_duplicada_falla(self):
        v = _serie_4h(3)
        with pytest.raises(ValueError):
            d.tramos_contiguos([v[1], v[0]])
        with pytest.raises(ValueError):
            d.tramos_contiguos([v[0], v[0]])

    def test_la_sma_coincide_con_la_de_la_plataforma_y_empieza_en_la_vela_600(self):
        velas = _serie_4h(700)
        obs = d.serie_distancias(velas)
        assert len(obs) == 700 - 599 and obs[0].posicion == 600 and obs[-1].posicion == 700
        cierres = [v.close for v in velas]
        referencia = sma_de_la_plataforma(cierres, 200)
        for o in obs:
            i = velas.index(next(v for v in velas if v.open_time == o.open_time))
            # mismo cálculo; la suma deslizante puede diferir en el último dígito decimal
            assert abs(o.dist_sma - (cierres[i] / referencia[i] - 1)) < Decimal("1e-20")

    def test_la_ema_es_recursiva_y_se_siembra_con_la_sma_de_los_200_primeros(self):
        velas = _serie_4h(650)
        cierres = [v.close for v in velas]
        ema = sum(cierres[:200], Decimal(0)) / 200
        esperadas = {}
        for i in range(200, 650):
            ema = ema + d.ALFA_EMA * (cierres[i] - ema)
            esperadas[velas[i].open_time] = cierres[i] / ema - 1
        obs = d.serie_distancias(velas)
        assert obs and all(o.dist_ema == esperadas[o.open_time] for o in obs)

    def test_un_hueco_reinicia_la_media_y_la_posicion(self):
        # tramo 1 de 700 velas (elegible desde la 600), hueco, tramo 2 de 650
        obs = d.serie_distancias(_serie_4h(1350, saltos=(699,)))
        assert len(obs) == (700 - 599) + (650 - 599)
        primera_del_segundo = [o for o in obs if o.posicion == 600][1]
        assert primera_del_segundo.open_time == T0 + CUATRO_HORAS * (700 + 1 + 599)

    def test_tramo_demasiado_corto_no_produce_observaciones(self):
        assert d.serie_distancias(_serie_4h(599)) == []


class TestPercentilMovil:
    def test_las_primeras_1080_no_se_clasifican(self):
        res = d.clasificar([Decimal(i) for i in range(1100)])
        assert all(r == (None, None) for r in res[:CALENTADA_PERCENTIL])
        assert res[CALENTADA_PERCENTIL][0] is not None

    def test_un_maximo_nuevo_es_cola_alta_y_un_minimo_nuevo_es_cola_baja(self):
        sube = d.clasificar([Decimal(i) for i in range(1100)])
        assert sube[-1] == (Decimal(100), Grupo.COLA_ALTA)
        baja = d.clasificar([Decimal(-i) for i in range(1100)])
        assert baja[-1] == (Decimal(0), Grupo.COLA_BAJA)

    def test_un_valor_central_es_centro(self):
        res = d.clasificar([Decimal(i) for i in range(1080)] + [Decimal("540.5")])
        pct, grupo = res[-1]
        assert grupo is Grupo.CENTRO and pct == Decimal(100) * 541 / 1080

    def test_los_bordes_de_las_colas_incluyen_el_5_y_el_95(self):
        assert d.grupo_de_percentil(Decimal(5)) is Grupo.COLA_BAJA
        assert d.grupo_de_percentil(Decimal("5.01")) is Grupo.CENTRO
        assert d.grupo_de_percentil(Decimal(95)) is Grupo.COLA_ALTA
        assert d.grupo_de_percentil(Decimal("94.99")) is Grupo.CENTRO


class TestSinInformacionFutura:
    """Control 3 del protocolo: `dist_t`, `pct_t` y el grupo de `t` no cambian si se alteran
    o eliminan las velas posteriores a `t`."""

    @pytest.fixture(scope="class")
    def velas(self):
        return _serie_4h(1900)

    @staticmethod
    def _clasificadas(velas):
        return d.clasificar_serie(d.serie_distancias(velas))

    @pytest.mark.parametrize("corte", [1700, 1800, 1899])
    def test_truncar_el_futuro_no_cambia_nada_hasta_t(self, velas, corte):
        completa = self._clasificadas(velas)
        truncada = self._clasificadas(velas[: corte + 1])
        assert truncada == completa[: len(truncada)]
        assert len(truncada) == corte + 1 - 599

    def test_alterar_el_futuro_no_cambia_nada_hasta_t(self, velas):
        corte = 1750
        alteradas = list(velas[: corte + 1])
        rng = random.Random(9)
        for v in velas[corte + 1 :]:
            c = v.close * Decimal(rng.choice([Decimal("0.5"), Decimal(3)]))
            alteradas.append(Vela4h(v.open_time, c, c, c, c))
        a = self._clasificadas(velas)
        b = self._clasificadas(alteradas)
        hasta_t = corte + 1 - 599
        assert a[:hasta_t] == b[:hasta_t]
        assert a[hasta_t:] != b[hasta_t:]  # el control no es trivial: el futuro sí cambió


class TestRetornosPosteriores:
    def test_retorno_exacto_y_solo_si_existe_la_vela_h_pasos_despues(self):
        velas = _serie_4h(10)
        r = d.retornos_posteriores(velas, 3)
        assert len(r) == 7
        assert r[velas[0].open_time] == velas[3].close / velas[0].close - 1
        assert velas[8].open_time not in r

    def test_un_hueco_impide_el_retorno_en_vez_de_rellenarlo(self):
        velas = _serie_4h(10, saltos=(4,))  # t0..t4, luego falta t5
        r = d.retornos_posteriores(velas, 3)
        assert velas[0].open_time in r  # t0 + 3 pasos = t3 existe
        assert velas[2].open_time not in r  # t2 + 3 pasos = t5: cae en el hueco, no se rellena


class TestEpisodios:
    @staticmethod
    def _puntos(indices):
        return [(T0 + CUATRO_HORAS * i, Decimal(i)) for i in indices]

    def test_velas_seguidas_son_un_episodio(self):
        e = d.agrupar_episodios(self._puntos([0, 1, 2, 3]), alto=True)
        assert len(e) == 1 and e[0].velas == 4 and e[0].extremo == 3

    def test_seis_velas_fuera_abren_un_episodio_nuevo_y_cinco_no(self):
        assert len(d.agrupar_episodios(self._puntos([0, 6]), alto=True)) == 1  # 5 fuera entre ambas
        assert len(d.agrupar_episodios(self._puntos([0, 7]), alto=True)) == 2  # 6 fuera entre ambas

    def test_el_extremo_del_lado_bajo_es_el_minimo(self):
        puntos = [(T0, Decimal("-0.3")), (T0 + CUATRO_HORAS, Decimal("-0.4"))]
        assert d.agrupar_episodios(puntos, alto=False)[0].extremo == Decimal("-0.4")


class TestParteA:
    def test_distribucion_por_rango_mas_cercano(self):
        dist = d.distribucion([Decimal(i) for i in range(1, 101)])
        assert dist.minimo == 1 and dist.p50 == 50 and dist.p95 == 95 and dist.p99 == 99 and dist.maximo == 100
        assert d.distribucion([]) is None

    def test_distribucion_por_anio_calendario_utc(self):
        obs = [_obs(date(2025, 12, 31), Decimal("0.01")), _obs(date(2026, 1, 1), Decimal("0.05"))]
        por_anio = d.distribucion_por_anio(obs, "dist_sma")
        assert list(por_anio) == [2025, 2026] and por_anio[2026].maximo == Decimal("0.05")

    def test_los_umbrales_son_estrictos(self):
        obs = [_obs(date(2026, 1, 1), Decimal("0.19")), _obs(date(2026, 1, 2), Decimal("0.1901")),
               _obs(date(2026, 1, 3), Decimal("-0.25")), _obs(date(2026, 1, 4), Decimal("-0.2501"))]
        r = d.rupturas_de_umbral(obs)
        assert (r.velas_altas, r.velas_bajas, r.velas_total) == (1, 1, 4)

    def test_ventanas_anuales_sin_ruptura(self):
        base = date(2025, 1, 1)
        obs = [_obs(base + timedelta(days=k), Decimal("0.01")) for k in range(400)]
        obs[5] = _obs(base + timedelta(days=5), Decimal("0.30"))
        v = d.ventanas_anuales_sin_ruptura(obs)
        assert v.ventanas == 36  # inicios de 0 a 35 (400 − 365 + 1)
        assert v.sin_ruptura == 30  # los inicios 0..5 contienen el día 5
        assert v.dias_con_ruptura == (base + timedelta(days=5),)

    def test_muestra_mas_corta_que_un_anio_no_tiene_ventanas(self):
        obs = [_obs(date(2025, 1, 1) + timedelta(days=k), Decimal("0.01")) for k in range(100)]
        assert d.ventanas_anuales_sin_ruptura(obs).ventanas == 0


class TestBootstrapYVeredicto:
    @staticmethod
    def _dias(n, efecto, ruido=Decimal("0.001")):
        rng = random.Random(3)
        dias = []
        for _ in range(n):
            a = [efecto + Decimal(rng.randint(-5, 5)) * ruido for _ in range(3)]
            b = [Decimal(rng.randint(-5, 5)) * ruido for _ in range(3)]
            dias.append((sum(a, Decimal(0)), 3, sum(b, Decimal(0)), 3))
        return dias

    def test_es_determinista(self):
        dias = self._dias(120, Decimal("0.02"))
        assert d.bootstrap_dif_de_medias(dias) == d.bootstrap_dif_de_medias(dias)

    def test_un_efecto_claro_excluye_el_cero_y_el_nulo_lo_incluye(self):
        con = d.bootstrap_dif_de_medias(self._dias(120, Decimal("0.02")))
        assert con.ci_inferior > 0 and con.ci_inferior <= con.media <= con.ci_superior
        sin = d.bootstrap_dif_de_medias(self._dias(120, Decimal(0)))
        assert sin.ci_inferior <= 0 <= sin.ci_superior

    def test_sin_valores_en_un_grupo_no_hay_intervalo(self):
        assert d.bootstrap_dif_de_medias([]) is None
        assert d.bootstrap_dif_de_medias([(Decimal(1), 1, Decimal(0), 0)] * 5) is None

    def test_el_bloque_es_de_14_dias_y_el_bootstrap_remuestrea_dias_enteros(self):
        # Con 10 días (< 14) el bloque se acota al total: la muestra es la serie completa remuestreada
        iv = d.bootstrap_dif_de_medias(self._dias(10, Decimal("0.05")))
        assert iv.n == 10 and iv.ci_inferior > 0

    @pytest.mark.parametrize(
        "cola,episodios,ic,esperado",
        [
            (Grupo.COLA_BAJA, 9, (1, 2), VeredictoD1.NO_CONCLUYENTE_MUESTRA_INSUFICIENTE),
            (Grupo.COLA_BAJA, 10, None, VeredictoD1.NO_CONCLUYENTE_MUESTRA_INSUFICIENTE),
            (Grupo.COLA_BAJA, 10, (-1, 2), VeredictoD1.SIN_EVIDENCIA),
            (Grupo.COLA_BAJA, 10, (0, 2), VeredictoD1.SIN_EVIDENCIA),  # tocar el 0 no excluye
            (Grupo.COLA_BAJA, 10, (1, 2), VeredictoD1.EN_LA_DIRECCION_ESPERADA),
            (Grupo.COLA_BAJA, 10, (-2, -1), VeredictoD1.EN_CONTRA),
            (Grupo.COLA_ALTA, 10, (-2, -1), VeredictoD1.EN_LA_DIRECCION_ESPERADA),
            (Grupo.COLA_ALTA, 10, (1, 2), VeredictoD1.EN_CONTRA),
            (Grupo.COLA_ALTA, 10, (-1, 1), VeredictoD1.SIN_EVIDENCIA),
        ],
    )
    def test_veredicto(self, cola, episodios, ic, esperado):
        iv = None if ic is None else IntervaloBootstrap(50, Decimal(0), Decimal(ic[0]), Decimal(ic[1]))
        assert d.veredicto_de(cola, episodios, iv) is esperado


class TestContraste:
    @staticmethod
    def _muestra(cada=5, dias=90, r_cola=Decimal("0.02"), r_centro=Decimal(0)):
        """90 días × 6 velas; la cola es el día entero cada `cada` días (episodios separados)."""
        clasif, retornos = [], {}
        for k in range(dias):
            en_cola = k % cada == 0
            for j in range(6):
                t = T0 + timedelta(days=k) + CUATRO_HORAS * j
                o = ObsDistancia(t, Decimal(1), Decimal("0.3") if en_cola else Decimal(0), Decimal(0), 600)
                g = Grupo.COLA_ALTA if en_cola else Grupo.CENTRO
                clasif.append(ObsClasificada(o, Decimal(99), g, None, None))
                retornos[t] = r_cola if en_cola else r_centro
        return clasif, retornos

    def test_diferencia_de_medias_episodios_y_veredicto(self):
        clasif, retornos = self._muestra()
        c = d.contraste(clasif, retornos, ma="SMA", cola=Grupo.COLA_ALTA, horizonte=18, grupo_de=d.grupo_sma)
        assert c.velas_cola == 18 * 6 and c.velas_centro == 72 * 6 and c.episodios == 18 and c.dias == 90
        assert c.intervalo.media == Decimal("0.02")
        # tras la cola ALTA la hipótesis esperaba un retorno MENOR; aquí es mayor → en contra
        assert c.veredicto is VeredictoD1.EN_CONTRA

    def test_pocos_episodios_no_concluyen_aunque_la_diferencia_sea_grande(self):
        clasif, retornos = self._muestra(cada=10)  # 9 episodios
        c = d.contraste(clasif, retornos, ma="SMA", cola=Grupo.COLA_ALTA, horizonte=18, grupo_de=d.grupo_sma)
        assert c.episodios == 9 and c.veredicto is VeredictoD1.NO_CONCLUYENTE_MUESTRA_INSUFICIENTE

    def test_sin_retorno_posterior_la_observacion_no_entra_a_las_medias_pero_si_a_los_episodios(self):
        clasif, retornos = self._muestra()
        for o in clasif[-12:]:
            retornos.pop(o.obs.open_time, None)
        c = d.contraste(clasif, retornos, ma="SMA", cola=Grupo.COLA_ALTA, horizonte=18, grupo_de=d.grupo_sma)
        assert c.velas_cola + c.velas_centro == len(clasif) - 12

    def test_la_cola_opuesta_no_entra_en_ninguno_de_los_dos_grupos(self):
        clasif, retornos = self._muestra()
        o = clasif[1].obs
        clasif[1] = ObsClasificada(o, Decimal(1), Grupo.COLA_BAJA, None, None)
        c = d.contraste(clasif, retornos, ma="SMA", cola=Grupo.COLA_ALTA, horizonte=18, grupo_de=d.grupo_sma)
        assert c.velas_cola + c.velas_centro == len(clasif) - 1

    def test_la_cola_debe_ser_extrema(self):
        clasif, retornos = self._muestra()
        with pytest.raises(ValueError):
            d.contraste(clasif, retornos, ma="SMA", cola=Grupo.CENTRO, horizonte=18, grupo_de=d.grupo_sma)

    def test_sin_clasificadas_no_hay_intervalo(self):
        clasif, retornos = self._muestra()
        sin = [ObsClasificada(c.obs, None, None, None, None) for c in clasif]
        c = d.contraste(sin, retornos, ma="SMA", cola=Grupo.COLA_BAJA, horizonte=18, grupo_de=d.grupo_sma)
        assert c.intervalo is None and c.veredicto is VeredictoD1.NO_CONCLUYENTE_MUESTRA_INSUFICIENTE

    def test_umbral_fijo_usa_la_misma_muestra_clasificada_que_la_sma(self):
        o = ObsDistancia(T0, Decimal(1), Decimal("0.20"), Decimal(0), 600)
        assert d.grupo_umbral_fijo(ObsClasificada(o, None, None, None, None)) is None
        assert d.grupo_umbral_fijo(ObsClasificada(o, Decimal(50), Grupo.CENTRO, None, None)) is Grupo.COLA_ALTA
        o2 = ObsDistancia(T0, Decimal(1), Decimal("-0.26"), Decimal(0), 600)
        assert d.grupo_umbral_fijo(ObsClasificada(o2, Decimal(50), Grupo.CENTRO, None, None)) is Grupo.COLA_BAJA


class TestEvaluacionCompleta:
    @pytest.fixture(scope="class")
    def informe(self):
        return d.evaluar_d1(_serie_4h(2300))

    def test_estructura_y_conteos(self, informe):
        assert informe.cobertura.velas_4h == 2300
        assert informe.cobertura.observaciones == 2300 - 599
        assert informe.cobertura.clasificadas == informe.n_clasificadas == 2300 - 599 - 1080
        assert len(informe.primarios) == 2 and {p.horizonte for p in informe.primarios} == {18}
        assert {p.cola for p in informe.primarios} == {Grupo.COLA_BAJA, Grupo.COLA_ALTA}
        assert all(p.ma == "SMA" for p in informe.primarios)
        # 4 (SMA h6/h42) + 6 (EMA) + 6 (umbral fijo) + 4 (mitades)
        assert len(informe.secundarios) == 20
        assert {c.muestra for c in informe.secundarios} == {"TODA", "PRIMERA_MITAD", "SEGUNDA_MITAD"}

    def test_es_reproducible(self, informe):
        assert d.evaluar_d1(_serie_4h(2300)) == informe

    def test_el_conteo_de_cobertura_no_calcula_distancias_ni_retornos(self, monkeypatch):
        def prohibido(*a, **k):
            raise AssertionError("el modo conteo no debe calcular distancias ni retornos")

        monkeypatch.setattr(d, "serie_distancias", prohibido)
        monkeypatch.setattr(d, "retornos_posteriores", prohibido)
        c = d.contar_cobertura(_serie_4h(1700))
        assert c.observaciones == 1700 - 599 and c.clasificadas == 1700 - 599 - 1080
