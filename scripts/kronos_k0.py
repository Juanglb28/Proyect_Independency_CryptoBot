"""K0 del experimento Kronos (enmienda E6): viabilidad en ESTE equipo.

Dos pasos, en este orden:

1. Fijar los pesos (UNA vez, con red):
       docker compose --profile kronos run --rm kronos python -m scripts.kronos_k0 --descargar
   Descarga el modelo y el tokenizador a `data/kronos/pesos/`, resuelve la
   revisión exacta de Hugging Face y escribe `data/kronos/pesos.lock.json` con
   revisión + SHA-256 de cada archivo. Ese archivo se versiona.

2. Medir (SIN red; usa solo lo fijado):
       docker compose --profile kronos run --rm kronos python -m scripts.kronos_k0 --fechas 12 --trayectorias 8

Solo LEE velas de la base (transacción READ ONLY) y no escribe nada en ella. El
informe se imprime y se guarda en `data/kronos/k0-<fecha>.json`. Nada de esto
llega a una orden, una cantidad, un stop ni al score (P2).

Los valores por defecto de trayectorias/temperatura/top_p/semilla son SOLO para
K0 (los de la documentación de Kronos); se congelan por separado antes de K1.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.data.exchange_client import Candle
from app.domain.rules.kronos_signal import CORTE_PREENTRENAMIENTO, ParametrosKronos
from app.domain.symbols import ALLOWED_SYMBOLS
from app.experiments.kronos.k0 import ReporteK0, ejecutar_k0, formatear_reporte, instantes_de_muestra
from app.experiments.kronos.pesos import (
    RUTA_LOCK_POR_DEFECTO,
    LockPesos,
    PesosNoFijados,
    descargar_pesos,
    escribir_lock,
    leer_lock,
)
from app.experiments.kronos.predictor import PredictorDeTrayectorias

# Repositorios de Hugging Face por modelo (los de la tabla del README de Kronos).
_REPOS = {
    "small": ("NeoQuasar/Kronos-small", "NeoQuasar/Kronos-Tokenizer-base"),
    "base": ("NeoQuasar/Kronos-base", "NeoQuasar/Kronos-Tokenizer-base"),
    "mini": ("NeoQuasar/Kronos-mini", "NeoQuasar/Kronos-Tokenizer-2k"),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m scripts.kronos_k0", description=__doc__.split("\n\n")[0])
    p.add_argument("--descargar", action="store_true", help="Descarga y fija los pesos (único paso con red).")
    p.add_argument("--modelo", choices=sorted(_REPOS), default="small")
    p.add_argument("--carpeta-pesos", type=Path, default=Path("data/kronos/pesos"))
    p.add_argument("--lock", type=Path, default=RUTA_LOCK_POR_DEFECTO)
    p.add_argument("--simbolos", nargs="+", default=None, help="Subconjunto de la lista autorizada (por defecto, todos).")
    p.add_argument("--fechas", type=int, default=12, help="Cuántos instantes de decisión (00:00 UTC) muestrear.")
    p.add_argument("--desde", type=date.fromisoformat, default=CORTE_PREENTRENAMIENTO.date())
    p.add_argument("--hasta", type=date.fromisoformat, default=None)
    p.add_argument("--lookback", type=int, default=400)
    p.add_argument("--horizonte", type=int, default=24)
    p.add_argument("--trayectorias", type=int, default=8)
    p.add_argument("--temperatura", type=Decimal, default=Decimal("1.0"))
    p.add_argument("--top-p", type=Decimal, default=Decimal("0.9"))
    p.add_argument("--semilla", type=int, default=20261015)
    p.add_argument("--dispositivo", default="cpu")
    p.add_argument("--sin-reproducibilidad", action="store_true")
    p.add_argument("--permitir-anterior-al-corte", action="store_true",
                   help="Diagnóstico de contaminación: acepta fechas < 2024-07-01 (marcadas como NO válidas).")
    p.add_argument("--salida", type=Path, default=None)
    return p.parse_args(argv)


def validar_simbolos(simbolos: list[str] | None) -> list[str]:
    """Solo símbolos de la lista autorizada (restricción 7 del master prompt)."""
    elegidos = list(ALLOWED_SYMBOLS) if simbolos is None else [s.upper() for s in simbolos]
    fuera = [s for s in elegidos if s not in ALLOWED_SYMBOLS]
    if fuera:
        raise SystemExit(f"Símbolos fuera de la lista autorizada: {fuera}")
    return elegidos


def parametros_desde(args: argparse.Namespace, lock: LockPesos) -> ParametrosKronos:
    return ParametrosKronos(
        model_id=lock.modelo.repo_id,
        tokenizer_id=lock.tokenizador.repo_id,
        weights_revision=lock.modelo.revision,
        tokenizer_revision=lock.tokenizador.revision,
        code_commit=lock.code_commit,
        lookback=args.lookback,
        horizon=args.horizonte,
        n_trajectories=args.trayectorias,
        temperature=args.temperatura,
        top_p=args.top_p,
        seed=args.semilla,
    )


def commit_del_codigo() -> str:
    """Commit de Kronos con el que se construyó la imagen (`/opt/kronos/.commit`)."""
    origen = Path(os.environ.get("KRONOS_SRC", "/opt/kronos"))
    archivo = origen / ".commit"
    if not archivo.is_file():
        raise SystemExit(f"No se encontró {archivo}: la imagen debe construirse con el commit fijado (Dockerfile.kronos).")
    return archivo.read_text(encoding="utf-8").strip()


def reporte_a_json(r: ReporteK0) -> dict:
    def conv(x):
        if isinstance(x, Decimal):
            return str(x)
        if isinstance(x, datetime):
            return x.isoformat()
        if dataclasses.is_dataclass(x) and not isinstance(x, type):
            return {k: conv(v) for k, v in dataclasses.asdict(x).items()}
        if isinstance(x, dict):
            return {k: conv(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [conv(v) for v in x]
        return x

    return conv(r)


def versiones_del_entorno() -> dict[str, str]:  # pragma: no cover - depende de lo instalado
    """Versiones que influyen en los números (se guardan junto al informe)."""
    from importlib import metadata

    out = {"python": sys.version.split()[0]}
    for paquete in ("torch", "numpy", "pandas", "huggingface_hub", "safetensors"):
        try:
            out[paquete] = metadata.version(paquete)
        except metadata.PackageNotFoundError:
            out[paquete] = "no instalado"
    return out


def _mostrar_progreso(hechos: int, total: int, segundos: float) -> None:  # pragma: no cover - salida a consola
    restante = segundos / hechos * (total - hechos)
    print(f"  pronóstico {hechos}/{total} · {segundos:.0f} s transcurridos · ~{restante / 60:.1f} min restantes", flush=True)


def ejecutar(
    args: argparse.Namespace,
    *,
    predictor: PredictorDeTrayectorias,
    velas_1h: dict[str, list[Candle]],
    lock: LockPesos,
    ahora: datetime,
    progreso=None,
) -> ReporteK0:
    """Núcleo testeable: sin red, sin base y sin torch (el predictor se inyecta)."""
    if args.desde < CORTE_PREENTRENAMIENTO.date() and not args.permitir_anterior_al_corte:
        raise SystemExit(
            "--desde es anterior al corte de preentrenamiento (2024-07-01): Kronos pudo ver esos datos. "
            "Usa --permitir-anterior-al-corte solo para un diagnóstico de contaminación."
        )
    hasta = args.hasta or (ahora.date() - timedelta(days=args.horizonte // 24 + 3))
    instantes = instantes_de_muestra(args.desde, hasta, args.fechas)
    return ejecutar_k0(
        velas_1h,
        predictor=predictor,
        params=parametros_desde(args, lock),
        instantes=instantes,
        dispositivo=args.dispositivo,
        comprobar_reproducibilidad=not args.sin_reproducibilidad,
        progreso=progreso,
    )


def _descargar(args: argparse.Namespace) -> int:  # pragma: no cover - requiere red
    repo_modelo, repo_tok = _REPOS[args.modelo]
    carpeta = args.carpeta_pesos
    modelo = descargar_pesos(repo_modelo, revision=None, destino=carpeta / "modelo")
    tokenizador = descargar_pesos(repo_tok, revision=None, destino=carpeta / "tokenizador")
    escribir_lock(LockPesos(modelo, tokenizador, commit_del_codigo()), args.lock)
    print(f"Pesos fijados en {args.lock}:")
    print(f"  {modelo.repo_id} @ {modelo.revision} ({len(modelo.archivos)} archivos)")
    print(f"  {tokenizador.repo_id} @ {tokenizador.revision} ({len(tokenizador.archivos)} archivos)")
    print("Versiona ese archivo. Las corridas siguientes funcionan SIN red y verifican estos hashes.")
    return 0


def _cargar_velas(simbolos: list[str]) -> dict[str, list[Candle]]:  # pragma: no cover - requiere base
    from app.config import get_settings
    from app.db.session import get_engine, get_sessionmaker
    from app.repositories.candles import get_candles
    from app.repositories.health import set_transaction_read_only

    session = get_sessionmaker(get_engine(get_settings()))()
    try:
        set_transaction_read_only(session)  # esta herramienta no puede escribir en la base
        return {s: get_candles(session, s, "1h") for s in simbolos}
    finally:
        session.rollback()
        session.close()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - requiere pesos reales
    args = parse_args(argv)
    if not args.descargar:
        os.environ["HF_HUB_OFFLINE"] = "1"  # uso sin red: solo lo fijado
    if args.descargar:
        return _descargar(args)
    try:
        lock = leer_lock(args.lock)
    except PesosNoFijados as e:
        print(str(e), file=sys.stderr)
        return 2
    if lock.code_commit != commit_del_codigo():
        print("El commit de Kronos de la imagen no coincide con el del bloqueo; reconstruye la imagen.", file=sys.stderr)
        return 2
    from app.experiments.kronos.predictor import KronosTorchPredictor

    simbolos = validar_simbolos(args.simbolos)
    predictor = KronosTorchPredictor(
        lock,
        carpeta_modelo=args.carpeta_pesos / "modelo",
        carpeta_tokenizador=args.carpeta_pesos / "tokenizador",
        device=args.dispositivo,
    )
    ahora = datetime.now(timezone.utc)
    reporte = ejecutar(
        args, predictor=predictor, velas_1h=_cargar_velas(simbolos), lock=lock, ahora=ahora, progreso=_mostrar_progreso
    )
    for linea in formatear_reporte(reporte):
        print(linea)
    salida = args.salida or Path(f"data/kronos/k0-{ahora:%Y%m%d-%H%M}.json")
    salida.parent.mkdir(parents=True, exist_ok=True)
    contenido = {"entorno": versiones_del_entorno(), **reporte_a_json(reporte)}
    salida.write_text(json.dumps(contenido, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Informe completo: {salida}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
