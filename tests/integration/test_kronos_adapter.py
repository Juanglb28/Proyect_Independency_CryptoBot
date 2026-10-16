"""Adaptador real de Kronos con un modelo DIMINUTO de pesos aleatorios.

No mide nada de rendimiento: solo comprueba el cableado (formato de entrada,
N trayectorias por contexto, semilla, verificación de los pesos). Se omite
solo si no hay torch o no está el código de Kronos (`KRONOS_SRC`), de modo que
corre dentro de la imagen del experimento:

    docker compose --profile kronos run --rm kronos python -m pytest tests/integration/test_kronos_adapter.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("pandas")
pytest.importorskip("einops")
pytest.importorskip("huggingface_hub")

_SRC = Path(os.environ.get("KRONOS_SRC", "/opt/kronos"))
if not (_SRC / "model" / "kronos.py").is_file():
    pytest.skip("No está el código de Kronos (KRONOS_SRC).", allow_module_level=True)

from app.domain.rules.kronos_signal import ParametrosKronos  # noqa: E402
from app.experiments.kronos.contexto import construir_contexto  # noqa: E402
from app.experiments.kronos.k0 import ejecutar_k0  # noqa: E402
from app.experiments.kronos.pesos import LockPesos, PesoFijado, PesosNoFijados, hashes_de_carpeta  # noqa: E402
from app.experiments.kronos.predictor import KronosTorchPredictor  # noqa: E402

D = Decimal
H = timedelta(hours=1)
AS_OF = datetime(2025, 3, 1, tzinfo=timezone.utc)
REV = "c" * 40
LOOKBACK, HORIZON = 64, 8


@pytest.fixture(scope="module")
def pesos_aleatorios(tmp_path_factory):
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    from model import Kronos, KronosTokenizer

    torch.manual_seed(0)
    raiz = tmp_path_factory.mktemp("kronos_tiny")
    tok = KronosTokenizer(
        d_in=6, d_model=16, n_heads=2, ff_dim=32, n_enc_layers=1, n_dec_layers=1, ffn_dropout_p=0.0,
        attn_dropout_p=0.0, resid_dropout_p=0.0, s1_bits=4, s2_bits=4, beta=0.05, gamma0=1.0, gamma=1.1,
        zeta=1.0, group_size=4,
    )
    mod = Kronos(
        s1_bits=4, s2_bits=4, n_layers=1, d_model=16, n_heads=2, ff_dim=32, ffn_dropout_p=0.0,
        attn_dropout_p=0.0, resid_dropout_p=0.0, token_dropout_p=0.0, learn_te=True,
    )
    tok.save_pretrained(raiz / "tok")
    mod.save_pretrained(raiz / "mod")
    lock = LockPesos(
        modelo=PesoFijado("local/tiny", REV, hashes_de_carpeta(raiz / "mod")),
        tokenizador=PesoFijado("local/tiny-tok", REV, hashes_de_carpeta(raiz / "tok")),
        code_commit="d" * 40,
    )
    return raiz, lock


def _params(**kw):
    base = dict(
        model_id="local/tiny", tokenizer_id="local/tiny-tok", weights_revision=REV, tokenizer_revision=REV,
        code_commit="d" * 40, lookback=LOOKBACK, horizon=HORIZON, n_trajectories=3,
        temperature=D("1.0"), top_p=D("0.9"), seed=7,
    )
    base.update(kw)
    return ParametrosKronos(**base)


def _velas(n=LOOKBACK + HORIZON + 4):
    inicio = AS_OF - H * LOOKBACK
    out = []
    for i in range(n):
        c = D("100") + D(i % 7) / D(10)
        out.append({
            "symbol": "BTCUSDT", "timeframe": "1h", "open_time": inicio + H * i, "open": c, "high": c + D("0.5"),
            "low": c - D("0.5"), "close": c, "volume": D("10"), "cerrada": True,
        })
    return out


def _predictor(raiz, lock):
    return KronosTorchPredictor(lock, carpeta_modelo=raiz / "mod", carpeta_tokenizador=raiz / "tok", device="cpu")


def test_devuelve_n_trayectorias_por_contexto_y_es_reproducible_con_la_misma_semilla(pesos_aleatorios):
    raiz, lock = pesos_aleatorios
    pred = _predictor(raiz, lock)
    ctx = construir_contexto("BTCUSDT", _velas(), as_of=AS_OF, lookback=LOOKBACK, horizon=HORIZON)
    a = pred.cierres_finales([ctx, ctx], _params())
    b = pred.cierres_finales([ctx, ctx], _params())
    assert len(a) == 2 and all(len(x) == 3 for x in a)
    assert all(isinstance(v, Decimal) and v > 0 for x in a for v in x)
    assert a == b
    c = pred.cierres_finales([ctx, ctx], _params(seed=8))
    assert c != a  # otra semilla, otras trayectorias


def _otro_contexto():
    velas = [
        dict(v, symbol="ETHUSDT", open=v["open"] * D("1.3"), high=v["high"] * D("1.3"),
             low=v["low"] * D("1.3"), close=v["close"] * D("1.3"))
        for v in _velas()
    ]
    return construir_contexto("ETHUSDT", velas, as_of=AS_OF, lookback=LOOKBACK, horizon=HORIZON)


def test_el_pronostico_de_un_contexto_no_depende_de_sus_acompanantes_ni_del_orden(pesos_aleatorios):
    """Defecto hallado en la primera corrida real de K0: con un lote compartido, el
    mismo contexto daba otras trayectorias según con quién se procesara."""
    raiz, lock = pesos_aleatorios
    pred = _predictor(raiz, lock)
    a = construir_contexto("BTCUSDT", _velas(), as_of=AS_OF, lookback=LOOKBACK, horizon=HORIZON)
    b = _otro_contexto()
    sola = pred.cierres_finales([a], _params())[0]
    assert pred.cierres_finales([a, b], _params())[0] == sola
    assert pred.cierres_finales([b, a], _params())[1] == sola


def test_las_trayectorias_de_un_mismo_contexto_no_son_copias(pesos_aleatorios):
    raiz, lock = pesos_aleatorios
    ctx = construir_contexto("BTCUSDT", _velas(), as_of=AS_OF, lookback=LOOKBACK, horizon=HORIZON)
    finales = _predictor(raiz, lock).cierres_finales([ctx], _params(n_trajectories=4))[0]
    assert len(set(finales)) > 1


def test_k0_completo_con_el_predictor_real(pesos_aleatorios):
    raiz, lock = pesos_aleatorios
    r = ejecutar_k0(
        {"BTCUSDT": _velas()}, predictor=_predictor(raiz, lock), params=_params(), instantes=[AS_OF],
        dispositivo="cpu", comprobar_reproducibilidad=True,
    )
    assert r.contextos_utilizables == 1 and r.reproducible is True
    assert r.segundos_por_pronostico is not None and r.segundos_por_pronostico > 0


def test_unos_pesos_alterados_no_se_cargan(pesos_aleatorios):
    raiz, lock = pesos_aleatorios
    adulterado = LockPesos(
        modelo=PesoFijado(lock.modelo.repo_id, lock.modelo.revision, {k: "0" * 64 for k in lock.modelo.archivos}),
        tokenizador=lock.tokenizador, code_commit=lock.code_commit,
    )
    with pytest.raises(PesosNoFijados):
        _predictor(raiz, adulterado)
