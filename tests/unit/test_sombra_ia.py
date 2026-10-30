"""S1 (protocolo `docs/protocolo-metrica-sombra-ia.md`): reglas puras, desenlaces con velas
sintéticas, modo conteo sin espiar y huella del protocolo. Sin base, red ni IA."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.data.exit_simulation import retorno_marcado_a_mercado
from app.domain.rules import sombra_ia as s1
from app.domain.rules.sombra_ia import (
    CicloConConsolidador,
    FilaPrefiltroS1,
    NivelIA,
    PropuestaS1,
    UnidadBase,
    UnidadValorada,
    VeredictoS1,
    construir_unidades_base,
    contar_muestra,
    diferencias_pareadas_por_ciclo,
    evaluar_s1,
    nivel_de_tier,
    serie_diaria,
)
from app.experiments import sombra_ia as exp
from app.experiments.sombra_ia import ProtocoloModificado, correr, valorar_unidades, verificar_protocolo

D = Decimal
UTC = timezone.utc
RAIZ = Path(__file__).resolve().parents[2]


def _ciclo(i: int, rec: str = "invertir", hora=0, minuto=5) -> CicloConConsolidador:
    t = datetime(2026, 10, 7, hora, minuto, tzinfo=UTC) + timedelta(days=i)
    return CicloConConsolidador(f"c{i:03d}", t, rec, "v1")


class TestParametrosFijos:
    def test_los_parametros_del_protocolo_estan_fijos(self):
        """Cambiarlos tras ver resultados es otro experimento: el protocolo los congela."""
        assert (s1.S1_BLOQUE_DIAS, s1.S1_MIN_DIAS_PAREADOS, s1.S1_MIN_CICLOS_PAREADOS, s1.S1_MIN_INCLUIDOS) == (3, 30, 30, 30)
        assert (s1.S1_ITERACIONES, s1.S1_SEMILLA) == (1000, 20261005)

    def test_niveles_ordenados(self):
        assert [int(n) for n in NivelIA] == [0, 1, 2, 3]
        assert [n.name for n in NivelIA] == ["NO_INCLUIDO", "NINGUNO", "MAYOR_RIESGO", "NORMAL"]

    def test_un_tier_desconocido_no_se_adivina(self):
        for malo in (None, "OBSERVACION", "otro"):
            with pytest.raises(ValueError):
                nivel_de_tier(malo)
        assert nivel_de_tier("MAYOR_RIESGO") == NivelIA.MAYOR_RIESGO


class TestUnidadesBase:
    def test_une_prefiltro_con_la_decision_de_la_ia(self):
        c = _ciclo(0)
        pref = [
            FilaPrefiltroS1(c.ciclo_id, "AAAUSDT", "CANDIDATO", D("60")),
            FilaPrefiltroS1(c.ciclo_id, "BBBUSDT", "CANDIDATO", D("55")),
            FilaPrefiltroS1(c.ciclo_id, "CCCUSDT", "DESCARTADO_POR_PUNTAJE", D("10")),
            FilaPrefiltroS1(c.ciclo_id, "DDDUSDT", "SIN_DATOS", None),
        ]
        prop = [PropuestaS1(c.ciclo_id, "AAAUSDT", "NINGUNO", D("47.5"))]
        u = {x.symbol: x for x in construir_unidades_base([c], pref, prop)}
        assert set(u) == {"AAAUSDT", "BBBUSDT", "CCCUSDT"}  # SIN_DATOS no tiene entrada simulable
        assert (u["AAAUSDT"].nivel, u["AAAUSDT"].puntaje_ia) == (NivelIA.NINGUNO, D("47.5"))
        assert u["BBBUSDT"].nivel == NivelIA.NO_INCLUIDO and u["BBBUSDT"].puntaje_ia is None
        assert u["CCCUSDT"].es_candidato is False and u["CCCUSDT"].nivel is None

    def test_las_propuestas_de_observacion_no_son_decisiones_de_la_ia(self):
        c = _ciclo(0)
        pref = [FilaPrefiltroS1(c.ciclo_id, "AAAUSDT", "CANDIDATO", D("60"))]
        prop = [PropuestaS1(c.ciclo_id, "AAAUSDT", "OBSERVACION", D("60"))]
        (u,) = construir_unidades_base([c], pref, prop)
        assert u.nivel == NivelIA.NO_INCLUIDO

    def test_con_dos_propuestas_del_mismo_simbolo_gana_el_mayor_nivel(self):
        c = _ciclo(0)
        pref = [FilaPrefiltroS1(c.ciclo_id, "AAAUSDT", "CANDIDATO", D("60"))]
        prop = [
            PropuestaS1(c.ciclo_id, "AAAUSDT", "NINGUNO", D("48")),
            PropuestaS1(c.ciclo_id, "AAAUSDT", "NORMAL", D("56")),
        ]
        (u,) = construir_unidades_base([c], pref, prop)
        assert (u.nivel, u.puntaje_ia) == (NivelIA.NORMAL, D("56"))

    def test_ciclos_sin_consolidador_no_entran(self):
        c = _ciclo(0)
        pref = [FilaPrefiltroS1("otro-ciclo", "AAAUSDT", "CANDIDATO", D("60"))]
        assert construir_unidades_base([c], pref, []) == []

    def test_una_propuesta_con_tier_vacio_sobre_un_candidato_es_inconsistencia(self):
        c = _ciclo(0)
        pref = [FilaPrefiltroS1(c.ciclo_id, "AAAUSDT", "CANDIDATO", D("60"))]
        with pytest.raises(ValueError):
            construir_unidades_base([c], pref, [PropuestaS1(c.ciclo_id, "AAAUSDT", None, D("50"))])


def _mundo(n_ciclos: int, *, efecto: float, ruido: float = 0.02, semilla: int = 1, rec_no_invertir_cada: int = 0):
    """Ciclos diarios con 2 incluidos y 3 no incluidos; el retorno de los incluidos
    suma `efecto`. Devuelve unidades valoradas (para evaluar) y bases (para contar)."""
    rng = random.Random(semilla)
    valoradas: list[UnidadValorada] = []
    for i in range(n_ciclos):
        rec = "no_invertir" if rec_no_invertir_cada and i % rec_no_invertir_cada == 0 else "invertir"
        t = datetime(2026, 10, 7, 0, 5, tzinfo=UTC) + timedelta(days=i)
        for j in range(5):
            incluido = j < 2
            valoradas.append(
                UnidadValorada(
                    ciclo_id=f"c{i:03d}", started_at=t, symbol=f"S{j}USDT", es_candidato=True,
                    nivel=NivelIA.NINGUNO if incluido else NivelIA.NO_INCLUIDO,
                    puntaje_ia=D("47") if incluido else None, score_prefiltro=D("60"),
                    recomendacion=rec, prompt_version="v1",
                    retorno_neto=D(str(round(rng.gauss(0, ruido) + (efecto if incluido else 0), 6))),
                    resuelta=(j % 2 == 0),
                )
            )
    return valoradas


class TestConteo:
    def test_cuenta_pareados_y_exige_los_tres_minimos(self):
        c = contar_muestra(_mundo(29, efecto=0))
        assert (c.ciclos_pareados, c.dias_pareados, c.incluidos, c.no_incluidos) == (29, 29, 58, 87)
        assert not c.suficiente and len(c.faltan) == 2  # días y ciclos; incluidos (58) sí cumple
        c = contar_muestra(_mundo(30, efecto=0))
        assert c.suficiente and c.faltan == ()

    def test_un_ciclo_sin_ambos_grupos_no_es_pareado(self):
        solo_no = [u for u in _mundo(5, efecto=0) if u.ciclo_id == "c000" and u.no_incluido]
        c = contar_muestra(solo_no)
        assert c.ciclos == 1 and c.ciclos_pareados == 0 and c.dias_pareados == 0

    def test_dos_ciclos_el_mismo_dia_cuentan_como_un_dia(self):
        base = _mundo(1, efecto=0)
        t2 = base[0].started_at + timedelta(hours=6)
        otro = [UnidadValorada(**{**{f: getattr(u, f) for f in u.__dataclass_fields__}, "ciclo_id": "c999", "started_at": t2}) for u in base]
        c = contar_muestra(base + otro)
        assert c.ciclos_pareados == 2 and c.dias_pareados == 1


class TestEstadistica:
    def test_diferencia_pareada_es_media_incluidos_menos_media_no_incluidos_por_ciclo(self):
        u = _mundo(1, efecto=0)
        u = [UnidadValorada(**{**{f: getattr(x, f) for f in x.__dataclass_fields__}, "retorno_neto": D(r)})
             for x, r in zip(u, ["0.10", "0.20", "0.01", "0.02", "0.03"])]
        d = diferencias_pareadas_por_ciclo(u, lambda x: x.incluido, lambda x: x.no_incluido)
        assert d["c000"][1] == D("0.15") - D("0.02")

    def test_serie_diaria_promedia_ciclos_del_mismo_dia_y_ordena(self):
        d1, d2 = date(2026, 10, 8), date(2026, 10, 7)
        serie = serie_diaria({"a": (d1, D("0.04")), "b": (d1, D("0.02")), "c": (d2, D("0.10"))})
        assert serie == [(d2, D("0.10")), (d1, D("0.03"))]

    def test_con_efecto_real_discrimina(self):
        r = evaluar_s1(_mundo(40, efecto=0.06))
        assert r.veredicto == VeredictoS1.DISCRIMINA
        assert r.primario is not None and r.primario.ci_inferior > 0

    def test_sin_efecto_casi_nunca_discrimina(self):
        """Calibración: sin efecto real, el límite inferior del IC95 solo debe quedar por
        encima de 0 en una minoría pequeña de mundos (esperado ~2,5 % a una cola). No se
        fija una semilla «que salga bien»: se mide la tasa sobre muchas."""
        falsos = 0
        for semilla in range(60):
            u = [x for x in _mundo(40, efecto=0.0, semilla=semilla) if x.es_candidato]
            serie = serie_diaria(diferencias_pareadas_por_ciclo(u, lambda x: x.incluido, lambda x: x.no_incluido))
            falsos += s1.intervalo_de_serie(serie).ci_inferior > 0
        assert falsos <= 6, falsos  # 10 % de 60: holgura sobre lo esperado, pero detecta un IC descalibrado

    def test_con_muestra_insuficiente_es_no_concluyente_aunque_haya_efecto(self):
        r = evaluar_s1(_mundo(10, efecto=0.2))
        assert r.veredicto == VeredictoS1.NO_CONCLUYENTE
        assert r.notas

    def test_el_resultado_es_reproducible(self):
        a = evaluar_s1(_mundo(40, efecto=0.03, semilla=5))
        b = evaluar_s1(_mundo(40, efecto=0.03, semilla=5))
        assert a == b

    def test_secundarios_siempre_se_reportan_completos(self):
        r = evaluar_s1(_mundo(40, efecto=0.03, rec_no_invertir_cada=4))
        claves = [s.clave for s in r.secundarios]
        assert claves == [
            "no_invertir_vs_invertir", "escala_ordenada", "puntaje_dentro_de_incluidos", "candidatos_vs_resto",
            "tasa_de_acierto", "primario_solo_resueltas", "confusion_prefiltro", "composicion",
        ]
        por = {s.clave: s for s in r.secundarios}
        assert por["no_invertir_vs_invertir"].intervalo is not None
        assert por["primario_solo_resueltas"].detalle["unidades_resueltas"] == "120"  # j = 0, 2, 4 de 5 por ciclo
        assert por["candidatos_vs_resto"].intervalo is None  # el mundo sintético no tiene «resto»
        # el pre-filtro es igual en ambos grupos: no hay confusión
        assert por["confusion_prefiltro"].intervalo.media == 0

    def test_la_tasa_de_acierto_es_descriptiva(self):
        r = evaluar_s1(_mundo(40, efecto=0.5))
        det = {s.clave: s for s in r.secundarios}["tasa_de_acierto"].detalle
        assert "1.0000" in det["incluidos"]


class TestSinEspiar:
    def test_sin_los_minimos_no_se_cargan_velas_ni_se_calcula_nada(self):
        bases = [UnidadBase(**{f: getattr(u, f) for f in UnidadBase.__dataclass_fields__}) for u in _mundo(5, efecto=0.5)]
        llamadas = []

        def cargar(simbolos):
            llamadas.append(simbolos)
            raise AssertionError("no debía leer velas")

        r = correr(bases, cargar)
        assert llamadas == [] and r.evaluada is False and r.informe is None and not r.conteo.suficiente

    def test_con_los_minimos_si_evalua(self):
        bases = [UnidadBase(**{f: getattr(u, f) for f in UnidadBase.__dataclass_fields__}) for u in _mundo(30, efecto=0)]
        pedidos = []

        def cargar(simbolos):
            pedidos.append(simbolos)
            return {}

        r = correr(bases, cargar)
        assert r.evaluada and pedidos and r.descartes == {"sin_vela_de_entrada": 150}
        assert r.informe.veredicto == VeredictoS1.NO_CONCLUYENTE  # sin velas no hay muestra valorada


def _vela(t: datetime, o, h, l, c, symbol="AAAUSDT") -> dict:
    return {"symbol": symbol, "timeframe": "1h", "open_time": t, "open": D(o), "high": D(h), "low": D(l), "close": D(c), "volume": D(1)}


def _serie_plana(desde: datetime, n: int, precio="100"):
    return [_vela(desde + timedelta(hours=i), precio, precio, precio, precio) for i in range(n)]


def _base(started_at: datetime, symbol="AAAUSDT") -> UnidadBase:
    return UnidadBase("c1", started_at, symbol, True, NivelIA.NORMAL, D("56"), D("60"), "invertir")


class TestDesenlaces:
    ciclo = datetime(2026, 10, 7, 0, 5, tzinfo=UTC)

    def test_la_entrada_es_la_ultima_vela_cerrada_antes_del_ciclo(self):
        # el ciclo es 00:05 → entrada = cierre de la vela de las 23:00 del día anterior
        desde = datetime(2026, 10, 6, 20, tzinfo=UTC)
        velas = _serie_plana(desde, 12)
        (u,), desc = valorar_unidades([_base(self.ciclo)], {"AAAUSDT": velas})
        idx = [v["open_time"] for v in velas].index(datetime(2026, 10, 6, 23, tzinfo=UTC))
        assert u.retorno_neto == retorno_marcado_a_mercado(velas, idx) and desc == {}
        assert u.resuelta is False  # precio plano: no toca ni stop ni objetivo

    def test_stop_resuelve_la_salida(self):
        desde = datetime(2026, 10, 6, 20, tzinfo=UTC)
        velas = _serie_plana(desde, 4)  # hasta las 23:00 (entrada)
        velas.append(_vela(datetime(2026, 10, 7, 0, tzinfo=UTC), "100", "100", "90", "91"))  # cae -9 %
        velas += _serie_plana(datetime(2026, 10, 7, 1, tzinfo=UTC), 3, "91")
        (u,), _ = valorar_unidades([_base(self.ciclo)], {"AAAUSDT": velas})
        assert u.resuelta is True and u.retorno_neto < D("-0.08")

    def test_sin_vela_de_entrada_o_sin_velas_posteriores_se_descarta_y_se_cuenta(self):
        desde = datetime(2026, 10, 6, 20, tzinfo=UTC)
        (_, desc1) = (valorar_unidades([_base(self.ciclo)], {"AAAUSDT": _serie_plana(desde, 2)}))  # llega hasta las 21:00
        assert desc1 == {"sin_vela_de_entrada": 1}
        solo_entrada = _serie_plana(desde, 4)  # la última vela es justo la de entrada
        (v, desc2) = valorar_unidades([_base(self.ciclo)], {"AAAUSDT": solo_entrada})
        assert v == [] and desc2 == {"sin_velas_posteriores": 1}

    def test_velas_futuras_no_cambian_la_entrada(self):
        """Sin look-ahead: añadir velas posteriores puede cambiar el desenlace, nunca el precio de entrada."""
        desde = datetime(2026, 10, 6, 20, tzinfo=UTC)
        corta = _serie_plana(desde, 8)
        larga = corta + _serie_plana(desde + timedelta(hours=8), 40, "100")
        (a,), _ = valorar_unidades([_base(self.ciclo)], {"AAAUSDT": corta})
        (b,), _ = valorar_unidades([_base(self.ciclo)], {"AAAUSDT": larga})
        assert a.retorno_neto == b.retorno_neto  # plano: mismo desenlace con más historia futura


class TestHuellaDelProtocolo:
    def test_la_huella_registrada_coincide_con_el_texto_congelado(self):
        assert verificar_protocolo(RAIZ / exp.RUTA_PROTOCOLO, RAIZ / exp.RUTA_HUELLA)

    def test_cualquier_cambio_se_detecta(self, tmp_path):
        protocolo = tmp_path / "p.md"
        protocolo.write_bytes(b"texto\r\ncongelado\n")
        huella = tmp_path / "h.txt"
        huella.write_text(exp.huella_protocolo(protocolo) + "\n")
        assert verificar_protocolo(protocolo, huella)  # los saltos de línea están normalizados
        protocolo.write_bytes(b"texto\ncongelado!\n")
        with pytest.raises(ProtocoloModificado):
            verificar_protocolo(protocolo, huella)
