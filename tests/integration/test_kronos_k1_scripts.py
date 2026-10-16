"""Scripts de K1 (generar y evaluar) con predictor falso y la base de pruebas."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.symbols import ALLOWED_SYMBOLS
from app.experiments.kronos import k1
from app.experiments.kronos.k1 import PronosticoGuardado
from app.experiments.kronos.pesos import LockPesos, PesoFijado
from app.repositories.kronos_forecasts import listar_forecasts
from scripts import kronos_k1_evaluar as ev
from scripts import kronos_k1_generar as gen

D = Decimal
H = timedelta(hours=1)
REV = "a" * 40
A, B = ALLOWED_SYMBOLS[0], ALLOWED_SYMBOLS[1]


def _lock(rev=REV):
    return LockPesos(
        PesoFijado("NeoQuasar/Kronos-small", rev, {"m": "0" * 64}),
        PesoFijado("NeoQuasar/Kronos-Tokenizer-base", "b" * 40, {"t": "1" * 64}),
        "c" * 40,
    )


def _velas(symbol, desde, n):
    return [
        {"symbol": symbol, "timeframe": "1h", "open_time": desde + H * i, "open": D(100), "high": D(101), "low": D(99),
         "close": D(100) + D(i % 5) / 10, "volume": D(1), "cerrada": True}
        for i in range(n)
    ]


class PredictorFijo:
    def __init__(self):
        self.llamadas = 0

    def cierres_finales(self, contextos, params):
        self.llamadas += len(contextos)
        return [[c.ultimo_cierre * D("1.01") + D(i) / 100 for i in range(params.n_trajectories)] for c in contextos]


@pytest.fixture()
def entorno(tmp_path):
    protocolo = tmp_path / "protocolo.md"
    protocolo.write_text("protocolo de prueba v1", encoding="utf-8")
    inicio = datetime(2024, 7, 1, tzinfo=timezone.utc) - H * 400
    velas = {s: _velas(s, inicio, 400 + 24 * 6) for s in (A, B)}
    return {"protocolo": protocolo, "fechas": tmp_path / "fechas.json", "velas": velas}


def _correr(db_session, entorno, predictor, *, lock=None, conjunto="primaria", max_minutos=None):
    args = gen.parse_args(["--conjunto", conjunto] + ([] if max_minutos is None else ["--max-minutos", str(max_minutos)]))
    return gen.ejecutar(
        args, predictor=predictor, velas_1h=entorno["velas"], lock=lock or _lock(), session=db_session,
        ahora=datetime(2024, 7, 5, tzinfo=timezone.utc), entorno='{"torch": "test"}',
        ruta_fechas=entorno["fechas"], ruta_protocolo=entorno["protocolo"],
    )


def _filas(db_session):
    p = k1.parametros_k1(_lock())
    return listar_forecasts(db_session, weights_revision=REV, lookback=p.lookback, horizon=p.horizon,
                            n_trajectories=p.n_trajectories, seed=p.seed)


def test_genera_guarda_con_la_huella_del_protocolo_y_se_puede_reanudar(db_session, entorno):
    pred = PredictorFijo()
    r = _correr(db_session, entorno, pred)
    filas = _filas(db_session)
    assert r.generados == len(filas) > 0 and r.pendientes == 0
    assert {f.symbol for f in filas} == {A, B}
    assert all(f.protocol_sha256 == k1.huella_protocolo(entorno["protocolo"]) for f in filas)
    assert all(len(f.final_closes) == 8 and f.uses_volume is False and f.device == "cpu" for f in filas)
    llamadas = pred.llamadas
    r2 = _correr(db_session, entorno, pred)  # reanudar: no hay nada pendiente
    assert (r2.generados, r2.ya_existian) == (0, len(filas)) and pred.llamadas == llamadas
    assert len(_filas(db_session)) == len(filas)


def test_la_lista_de_fechas_se_fija_una_vez_y_no_crece_con_velas_nuevas(db_session, entorno):
    _correr(db_session, entorno, PredictorFijo())
    fijadas = json.loads(entorno["fechas"].read_text())["primarias"]
    inicio = datetime(2024, 7, 1, tzinfo=timezone.utc) - H * 400
    entorno["velas"] = {s: _velas(s, inicio, 400 + 24 * 30) for s in (A, B)}  # llegan más velas
    _correr(db_session, entorno, PredictorFijo())
    assert json.loads(entorno["fechas"].read_text())["primarias"] == fijadas


def test_si_el_protocolo_cambia_no_se_reanuda(db_session, entorno):
    _correr(db_session, entorno, PredictorFijo())
    entorno["protocolo"].write_text("protocolo MODIFICADO", encoding="utf-8")
    with pytest.raises(ValueError, match="experimento nuevo"):
        _correr(db_session, entorno, PredictorFijo())


def test_solo_se_pronostican_simbolos_de_la_lista_autorizada(db_session, entorno):
    entorno["velas"]["FAKEUSDT"] = _velas("FAKEUSDT", datetime(2024, 7, 1, tzinfo=timezone.utc) - H * 400, 600)
    _correr(db_session, entorno, PredictorFijo())
    assert "FAKEUSDT" not in {f.symbol for f in _filas(db_session)}


def test_el_limite_de_tiempo_detiene_y_luego_se_completa(db_session, entorno):
    pred = PredictorFijo()
    # 0 minutos: se detiene antes del primero; no guarda nada y deja todo pendiente.
    r = _correr(db_session, entorno, pred, max_minutos=0)
    assert r.detenido_por_tiempo and r.generados == 0 and r.pendientes > 0 and _filas(db_session) == []
    assert _correr(db_session, entorno, pred).pendientes == 0 and _filas(db_session)


# --- evaluar ---------------------------------------------------------------------

def _precio(h: float) -> Decimal:
    return Decimal(str(round(100 + h * 0.02 + 8 * math.sin(h / 60) + 3 * math.sin(h * 1.7), 6)))


def test_evaluar_avisa_si_la_generacion_esta_incompleta_y_produce_json():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    horarias = [
        {"symbol": "X", "timeframe": "1h", "open_time": t0 + H * h, "open": _precio(h), "high": _precio(h) * D("1.001"),
         "low": _precio(h) * D("0.999"), "close": _precio(h + 1), "volume": D(10)}
        for h in range(260 * 24)
    ]
    diarias = [
        {"symbol": "X", "timeframe": "1d", "open_time": t0 + timedelta(days=d), "open": _precio(d * 24), "high": _precio(d * 24 + 23) * D("1.01"),
         "low": _precio(d * 24) * D("0.99"), "close": _precio(d * 24 + 23), "volume": D(1000 + (d * 37) % 400)}
        for d in range(260)
    ]
    dia = datetime(2025, 7, 1, tzinfo=timezone.utc)
    cierre = next(v["close"] for v in horarias if v["open_time"] == dia - H)
    fijadas = {"primarias": [date(2025, 7, 1), date(2025, 7, 4)], "diagnostico": []}
    lineas, datos = ev.evaluar([PronosticoGuardado("X", dia, cierre, D("0.01"))], {"X": diarias}, {"X": horarias}, fechas_fijadas=fijadas)
    texto = "\n".join(lineas)
    assert "GENERACIÓN INCOMPLETA" in texto and "NO es el veredicto del protocolo" in texto
    assert "NO_CONCLUYENTE_MUESTRA_INSUFICIENTE" in texto and "DIAGNÓSTICO PRE-CORTE" in texto
    assert datos["fechas_primarias_sin_pronostico"] == 1
    json.dumps(datos)  # serializable


def test_los_descartes_se_atribuyen_a_su_propio_conjunto():
    """Antes se sumaban todos y salían bajo el bloque primario, aunque vinieran del diagnóstico."""
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    horarias = [
        {"symbol": "X", "timeframe": "1h", "open_time": t0 + H * h, "open": _precio(h), "high": _precio(h) * D("1.001"),
         "low": _precio(h) * D("0.999"), "close": _precio(h + 1), "volume": D(10)}
        for h in range(260 * 24)
    ]
    diarias = [
        {"symbol": "X", "timeframe": "1d", "open_time": t0 + timedelta(days=d), "open": _precio(d * 24), "high": _precio(d * 24 + 23) * D("1.01"),
         "low": _precio(d * 24) * D("0.99"), "close": _precio(d * 24 + 23), "volume": D(1000 + (d * 37) % 400)}
        for d in range(260)
    ]
    primario_malo = PronosticoGuardado("X", datetime(2025, 7, 1, tzinfo=timezone.utc), D("1"), D("0.01"))   # cierre inconsistente
    pre_corte = PronosticoGuardado("X", datetime(2024, 3, 1, tzinfo=timezone.utc), D("1"), D("0.01"))        # sin velas: sin observación
    lineas, datos = ev.evaluar([primario_malo, pre_corte], {"X": diarias}, {"X": horarias})
    assert datos["descartes"]["primario"] == {"cierre_inconsistente": 1}
    assert datos["descartes"]["pre_corte"] == {"sin_observacion_del_arnes": 1}
    bloques = "\n".join(lineas).split("=== ")
    primario = next(b for b in bloques if b.startswith("PRIMARIO"))
    diagnostico = next(b for b in bloques if b.startswith("DIAGNÓSTICO"))
    assert "cierre_inconsistente=1" in primario and "sin_observacion_del_arnes" not in primario
    assert "sin_observacion_del_arnes=1" in diagnostico and "cierre_inconsistente" not in diagnostico
