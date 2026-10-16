"""Fijación (pin) de los pesos de Kronos: revisión y hash de cada archivo.

Los pesos son código ejecutable en potencia (cadena de suministro, P4): se
descargan UNA vez, se anota en `data/kronos/pesos.lock.json` la revisión exacta y
el SHA-256 de cada archivo, y de ahí en adelante se usan SIN red
(`HF_HUB_OFFLINE=1`) y verificando que los hashes no cambiaron. Si el archivo de
bloqueo no existe, no se corre nada: no hay forma de "usar lo que haya".

Este módulo no importa torch ni huggingface_hub salvo dentro de `descargar_pesos`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

RUTA_LOCK_POR_DEFECTO = Path("data/kronos/pesos.lock.json")


@dataclass(frozen=True)
class PesoFijado:
    repo_id: str
    revision: str
    archivos: dict[str, str]  # nombre relativo -> sha256


@dataclass(frozen=True)
class LockPesos:
    modelo: PesoFijado
    tokenizador: PesoFijado
    code_commit: str


class PesosNoFijados(Exception):
    """Falta el archivo de bloqueo, o los archivos no coinciden con él."""


def sha256_de(ruta: Path) -> str:
    h = hashlib.sha256()
    with ruta.open("rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


def hashes_de_carpeta(carpeta: Path) -> dict[str, str]:
    """SHA-256 de cada archivo de la carpeta, ignorando los metadatos de caché de
    Hugging Face (`.cache/`) y archivos ocultos."""
    return {
        p.relative_to(carpeta).as_posix(): sha256_de(p)
        for p in sorted(carpeta.rglob("*"))
        if p.is_file() and ".cache" not in p.relative_to(carpeta).parts and not p.name.startswith(".")
    }


def escribir_lock(lock: LockPesos, ruta: Path = RUTA_LOCK_POR_DEFECTO) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    datos = {
        "code_commit": lock.code_commit,
        "modelo": {"repo_id": lock.modelo.repo_id, "revision": lock.modelo.revision, "archivos": lock.modelo.archivos},
        "tokenizador": {
            "repo_id": lock.tokenizador.repo_id,
            "revision": lock.tokenizador.revision,
            "archivos": lock.tokenizador.archivos,
        },
    }
    ruta.write_text(json.dumps(datos, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def leer_lock(ruta: Path = RUTA_LOCK_POR_DEFECTO) -> LockPesos:
    if not ruta.is_file():
        raise PesosNoFijados(
            f"No existe {ruta}. Descarga y fija los pesos primero (scripts.kronos_k0 --descargar); "
            "no se usan pesos sin revisión ni hash fijos."
        )
    datos = json.loads(ruta.read_text(encoding="utf-8"))

    def _peso(clave: str) -> PesoFijado:
        d = datos[clave]
        return PesoFijado(repo_id=d["repo_id"], revision=d["revision"], archivos=dict(d["archivos"]))

    return LockPesos(modelo=_peso("modelo"), tokenizador=_peso("tokenizador"), code_commit=datos["code_commit"])


def verificar_carpeta(carpeta: Path, esperado: PesoFijado) -> None:
    """Falla si la carpeta local no tiene EXACTAMENTE los archivos y hashes fijados."""
    actual = hashes_de_carpeta(carpeta)
    if actual != esperado.archivos:
        faltan = sorted(set(esperado.archivos) - set(actual))
        sobran = sorted(set(actual) - set(esperado.archivos))
        distintos = sorted(k for k in set(actual) & set(esperado.archivos) if actual[k] != esperado.archivos[k])
        raise PesosNoFijados(
            f"Los archivos de {esperado.repo_id} no coinciden con el bloqueo: faltan={faltan}, sobran={sobran}, "
            f"hash distinto={distintos}."
        )


def descargar_pesos(
    repo_id: str, *, revision: str | None, destino: Path
) -> PesoFijado:  # pragma: no cover - requiere red y huggingface_hub
    """Descarga `repo_id` en `destino`. Con `revision=None` resuelve la revisión
    ACTUAL a un hash y descarga ese hash exacto (nunca una rama móvil)."""
    from huggingface_hub import HfApi, snapshot_download

    api = HfApi()
    if revision is None:
        revision = api.model_info(repo_id).sha
    if not revision:
        raise PesosNoFijados(f"No se pudo resolver la revisión de {repo_id}.")
    snapshot_download(repo_id=repo_id, revision=revision, local_dir=str(destino))
    return PesoFijado(repo_id=repo_id, revision=revision, archivos=hashes_de_carpeta(Path(destino)))
