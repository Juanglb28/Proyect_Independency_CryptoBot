"""D1 — el estudio (módulo + script): huella del protocolo, modo conteo sin espiar, control de
agregación y salida. Sin base, red ni IA (las velas llegan por argumento)."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import app.experiments.distancia_ma200 as exp
from app.experiments.sombra_ia import ProtocoloModificado, huella_protocolo
from scripts import estudio_distancia_ma200 as script

RAIZ = Path(__file__).resolve().parents[2]
T0 = datetime(2022, 1, 1, tzinfo=timezone.utc)
UNA_HORA = timedelta(hours=1)


def _velas_1h_y_4h(n_4h: int, *, semilla: int = 1):
    """`n_4h` velas de 4 h sintéticas y sus velas de 1 h (agregan exactamente a cada 4 h)."""
    rng = random.Random(semilla)
    precio = Decimal(30000)
    uno, cuatro = [], []
    for i in range(n_4h):
        t = T0 + timedelta(hours=4 * i)
        precio = precio * (1 + Decimal(rng.randint(-150, 160)) / Decimal(10000))
        o, h, l, c = precio * Decimal("0.999"), precio * Decimal("1.004"), precio * Decimal("0.996"), precio
        cuatro.append({"open_time": t, "open": o, "high": h, "low": l, "close": c})
        medio = (o + c) / 2
        partes = [(o, medio, medio, medio), (medio, h, medio, medio), (medio, medio, l, medio), (medio, medio, medio, c)]
        for k, (ao, ah, al, ac) in enumerate(partes):
            uno.append({"open_time": t + UNA_HORA * k, "open": ao, "high": max(ah, ao, ac), "low": min(al, ao, ac), "close": ac})
    # el máximo y el mínimo de cada 4 h deben ser exactamente h y l
    return uno, cuatro


class TestHuellaDelProtocolo:
    def test_la_huella_registrada_coincide_con_el_texto_congelado(self):
        assert exp.verificar_protocolo(RAIZ / exp.RUTA_PROTOCOLO, RAIZ / exp.RUTA_HUELLA)

    def test_cambiar_el_texto_hace_fallar_la_verificacion(self, tmp_path):
        protocolo = tmp_path / "p.md"
        protocolo.write_text("# protocolo\n", encoding="utf-8")
        huella = tmp_path / "h.txt"
        huella.write_text(huella_protocolo(protocolo) + "\n")
        assert exp.verificar_protocolo(protocolo, huella)
        protocolo.write_text("# protocolo cambiado\n", encoding="utf-8")
        with pytest.raises(ProtocoloModificado):
            exp.verificar_protocolo(protocolo, huella)

    def test_el_texto_congelado_declara_los_mismos_valores_que_el_codigo(self):
        texto = (RAIZ / exp.RUTA_PROTOCOLO).read_text(encoding="utf-8")
        for fragmento in (
            "Aprobado por el usuario el 2026-11-17 y congelado",
            "1.080", "bloques de 14 días", "semilla 20261005", "1.000 iteraciones",
            "+19 %", "−25 %", "10 episodios", "{6, 18, 42}", "SMA", "EMA", "BTCUSDT",
        ):
            assert fragmento in texto, fragmento


class TestCorrida:
    def test_el_modo_conteo_no_evalua_nada(self, monkeypatch):
        def prohibido(*a, **k):
            raise AssertionError("el modo conteo no debe calcular distancias ni retornos")

        monkeypatch.setattr(exp, "evaluar_d1", prohibido)
        uno, cuatro = _velas_1h_y_4h(1700)
        r = exp.correr(uno, cuatro, solo_contar=True)
        assert not r.evaluada and r.informe is None
        assert r.cobertura.velas_4h == 1700 and r.cobertura.observaciones == 1700 - 599
        assert r.control.valido and r.control.comparadas == 1700

    def test_un_control_de_agregacion_fallido_impide_evaluar(self):
        uno, cuatro = _velas_1h_y_4h(700)
        cuatro[10] = {**cuatro[10], "close": cuatro[10]["close"] + 1}
        r = exp.correr(uno, cuatro)
        assert not r.evaluada and r.informe is None and "control" in r.motivo_sin_evaluar
        assert not r.control.valido and r.control.n_discrepancias == 1

    def test_sin_velas_de_4h_guardadas_el_control_no_aplica_pero_se_declara(self):
        uno, _ = _velas_1h_y_4h(700)
        r = exp.correr(uno, [], solo_contar=True)
        assert r.control.comparadas == 0
        assert any("sin control" in l for l in exp.formatear_cobertura(r.agregacion, r.cobertura, r.control))

    def test_pocas_velas_no_se_evaluan(self):
        uno, cuatro = _velas_1h_y_4h(500)
        r = exp.correr(uno, cuatro)
        assert not r.evaluada and "600" in r.motivo_sin_evaluar

    def test_corrida_completa_informe_y_json(self):
        uno, cuatro = _velas_1h_y_4h(2300)
        r = exp.correr(uno, cuatro)
        assert r.evaluada and r.informe is not None
        texto = "\n".join(exp.formatear_informe(r, titulo="PRUEBA"))
        for esperado in ("PARTE A", "PARTE B", "CONTRASTES PRIMARIOS", "SECUNDARIOS", "CONTROL 1", "CONTROL 2", "UMBRAL_FIJO"):
            assert esperado in texto, esperado
        datos = exp.informe_a_json(r, huella="h" * 64)
        assert json.loads(json.dumps(datos))["protocolo_sha256"] == "h" * 64
        assert len(datos["primarios"]) == 2 and len(datos["secundarios"]) == 20
        assert datos["control_agregacion"]["valido"] is True

    def test_el_primer_dato_incompleto_de_1h_se_cuenta_y_no_se_rellena(self):
        uno, cuatro = _velas_1h_y_4h(700)
        del uno[7]  # falta una hora dentro de la segunda vela de 4 h
        r = exp.correr(uno, cuatro, solo_contar=True)
        assert r.agregacion.bloques_incompletos == 1 and r.cobertura.velas_4h == 699
        assert r.control.solo_guardadas == 1 and r.control.valido


class TestScript:
    def test_salida_primaria_y_actualizaciones(self, tmp_path):
        ahora = datetime(2026, 11, 17, 12, 30, tzinfo=timezone.utc)
        assert script.nombre_de_salida(tmp_path, ahora) == "evaluacion-primaria-20261117-1230.json"
        (tmp_path / "evaluacion-primaria-20261117-1230.json").write_text("{}")
        assert script.nombre_de_salida(tmp_path, ahora) == "actualizacion-20261117-1230.json"

    def test_argumentos(self):
        assert script.parse_args(["--solo-contar"]).solo_contar is True
        assert script.parse_args([]).solo_contar is False
        assert script.parse_args([]).salida == Path("data/distancia_ma200")

    def test_el_simbolo_esta_autorizado_y_es_solo_btc(self):
        from app.domain.symbols import ALLOWED_SYMBOLS

        assert exp.SIMBOLO == "BTCUSDT" and exp.SIMBOLO in ALLOWED_SYMBOLS
