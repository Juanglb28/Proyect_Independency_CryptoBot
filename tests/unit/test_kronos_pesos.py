"""Fijación de pesos por revisión y SHA-256 (E6, cadena de suministro)."""

from __future__ import annotations

import pytest

from app.experiments.kronos.pesos import (
    LockPesos,
    PesoFijado,
    PesosNoFijados,
    escribir_lock,
    hashes_de_carpeta,
    leer_lock,
    verificar_carpeta,
)


def _carpeta(tmp_path, nombre, archivos):
    c = tmp_path / nombre
    for rel, contenido in archivos.items():
        ruta = c / rel
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(contenido)
    return c


def test_los_hashes_ignoran_la_cache_de_huggingface(tmp_path):
    c = _carpeta(tmp_path, "m", {"model.safetensors": b"abc", ".cache/huggingface/x.metadata": b"zzz", "config.json": b"{}"})
    assert sorted(hashes_de_carpeta(c)) == ["config.json", "model.safetensors"]


def test_lock_ida_y_vuelta_y_verificacion(tmp_path):
    c = _carpeta(tmp_path, "m", {"model.safetensors": b"pesos", "config.json": b"{}"})
    t = _carpeta(tmp_path, "t", {"model.safetensors": b"tok"})
    lock = LockPesos(
        modelo=PesoFijado("NeoQuasar/Kronos-small", "a" * 40, hashes_de_carpeta(c)),
        tokenizador=PesoFijado("NeoQuasar/Kronos-Tokenizer-base", "b" * 40, hashes_de_carpeta(t)),
        code_commit="c" * 40,
    )
    ruta = tmp_path / "data" / "pesos.lock.json"
    escribir_lock(lock, ruta)
    leido = leer_lock(ruta)
    assert leido == lock
    verificar_carpeta(c, leido.modelo)
    verificar_carpeta(t, leido.tokenizador)


def test_un_archivo_alterado_hace_fallar_la_verificacion(tmp_path):
    c = _carpeta(tmp_path, "m", {"model.safetensors": b"pesos"})
    fijado = PesoFijado("x/y", "a" * 40, hashes_de_carpeta(c))
    (c / "model.safetensors").write_bytes(b"otros pesos")
    with pytest.raises(PesosNoFijados, match="hash distinto"):
        verificar_carpeta(c, fijado)


def test_un_archivo_de_mas_o_de_menos_tambien_falla(tmp_path):
    c = _carpeta(tmp_path, "m", {"a.bin": b"1"})
    fijado = PesoFijado("x/y", "a" * 40, hashes_de_carpeta(c))
    (c / "extra.py").write_bytes(b"print('hola')")
    with pytest.raises(PesosNoFijados, match="sobran"):
        verificar_carpeta(c, fijado)
    (c / "extra.py").unlink()
    (c / "a.bin").unlink()
    with pytest.raises(PesosNoFijados, match="faltan"):
        verificar_carpeta(c, fijado)


def test_sin_archivo_de_bloqueo_no_se_corre_nada(tmp_path):
    with pytest.raises(PesosNoFijados, match="No existe"):
        leer_lock(tmp_path / "no-existe.json")
