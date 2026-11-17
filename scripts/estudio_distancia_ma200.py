"""D1 — distancia del precio a la media de 200 periodos (4 h) en BTCUSDT
(protocolo `docs/protocolo-distancia-ma200-4h.md`, v1).

    docker compose run --rm app python -m scripts.estudio_distancia_ma200 --solo-contar
    docker compose run --rm app python -m scripts.estudio_distancia_ma200

Solo LEE (transacción READ ONLY). No abre posiciones, no escribe en la base, no llama a la
IA ni a la red, y no toca el score, el pre-filtro ni ninguna regla. Dos modos:

1. `--solo-contar`: cobertura de datos y control de agregación (la vela de 4 h armada con
   velas de 1 h debe coincidir exactamente con la de 4 h guardada). NO calcula ninguna
   distancia ni ningún retorno.
2. Evaluación (sin flag): exige el control de agregación; si falla, no calcula nada más.
   La PRIMERA evaluación se guarda como `evaluacion-primaria-*.json`; las siguientes, como
   `actualizacion-*.json`, y no sustituyen a la primaria.

Se niega a trabajar si el texto del protocolo cambió respecto a su huella registrada.
Código de salida: 0 = bien; 2 = no se pudo evaluar (control fallido o sin datos); 3 = protocolo
modificado o sin huella.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.experiments.distancia_ma200 import (
    SIMBOLO,
    formatear_cobertura,
    formatear_informe,
    informe_a_json,
    correr,
    verificar_protocolo,
)
from app.experiments.sombra_ia import ProtocoloModificado

CARPETA_SALIDA = Path("data/distancia_ma200")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m scripts.estudio_distancia_ma200", description=__doc__.split("\n\n")[0])
    p.add_argument("--solo-contar", action="store_true", help="cobertura y control de agregación; sin distancias ni retornos")
    p.add_argument("--salida", type=Path, default=CARPETA_SALIDA)
    return p.parse_args(argv)


def nombre_de_salida(carpeta: Path, ahora: datetime) -> str:
    """La primera evaluación es la primaria; las demás, actualizaciones."""
    hay_primaria = any(carpeta.glob("evaluacion-primaria-*.json"))
    prefijo = "actualizacion" if hay_primaria else "evaluacion-primaria"
    return f"{prefijo}-{ahora.strftime('%Y%m%d-%H%M')}.json"


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - requiere base
    args = parse_args(argv)
    try:
        huella = verificar_protocolo()
    except (ProtocoloModificado, FileNotFoundError) as e:
        print(str(e), file=sys.stderr)
        return 3

    from app.config import get_settings
    from app.db.session import get_engine, get_sessionmaker
    from app.domain.symbols import ALLOWED_SYMBOLS
    from app.repositories.candles import get_candles
    from app.repositories.health import set_transaction_read_only

    if SIMBOLO not in ALLOWED_SYMBOLS:
        print(f"{SIMBOLO} no está en la lista de símbolos autorizados.", file=sys.stderr)
        return 2

    session = get_sessionmaker(get_engine(get_settings()))()
    try:
        set_transaction_read_only(session)
        velas_1h = get_candles(session, SIMBOLO, "1h")
        velas_4h = get_candles(session, SIMBOLO, "4h")
        resultado = correr(velas_1h, velas_4h, solo_contar=args.solo_contar)
    finally:
        session.rollback()
        session.close()

    print(f"Protocolo D1 v1 · huella {huella[:12]}…")
    if not resultado.evaluada:
        for l in formatear_cobertura(resultado.agregacion, resultado.cobertura, resultado.control):
            print(l)
        if args.solo_contar:
            print("\nModo conteo: no se calculó ninguna distancia ni ningún retorno.")
            return 0
        print(f"\nNO SE EVALÚA: {resultado.motivo_sin_evaluar}", file=sys.stderr)
        return 2

    ahora = datetime.now(timezone.utc)
    nombre = nombre_de_salida(args.salida, ahora)
    titulo = (
        "EVALUACIÓN PRIMARIA — protocolo D1 v1"
        if nombre.startswith("evaluacion-primaria")
        else "ACTUALIZACIÓN (no sustituye a la evaluación primaria) — protocolo D1 v1"
    )
    for l in formatear_informe(resultado, titulo=titulo):
        print(l)
    args.salida.mkdir(parents=True, exist_ok=True)
    ruta = args.salida / nombre
    ruta.write_text(json.dumps(informe_a_json(resultado, huella=huella), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nGuardado en {ruta}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
