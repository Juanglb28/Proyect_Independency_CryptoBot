"""S1 — métrica a la sombra del análisis de IA (protocolo `docs/protocolo-metrica-sombra-ia.md`).

    docker compose run --rm app python -m scripts.estudio_sombra_ia

Solo LEE (transacción READ ONLY). No abre posiciones, no escribe en la base, no llama a
la IA ni a la red. Dos modos, decididos por el propio protocolo:

1. MODO CONTEO (mientras los mínimos de muestra no se cumplan): imprime cuántos días,
   ciclos pareados y candidatos incluidos hay. NO lee velas posteriores ni calcula ningún
   desenlace, para no espiar resultados.
2. EVALUACIÓN (cuando se cumplen los tres mínimos): calcula el contraste primario y los
   secundarios con las reglas fijadas. La PRIMERA evaluación se guarda como
   `evaluacion-primaria-*.json`; las siguientes, como `actualizacion-*.json`, y no
   sustituyen a la primaria.

Se niega a trabajar si el texto del protocolo cambió respecto a su huella registrada.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.domain.rules.sombra_ia import (
    CicloConConsolidador,
    FilaPrefiltroS1,
    PropuestaS1,
    construir_unidades_base,
)
from app.experiments.sombra_ia import (
    ProtocoloModificado,
    correr,
    formatear_conteo,
    formatear_informe,
    informe_a_json,
    verificar_protocolo,
)

CARPETA_SALIDA = Path("data/sombra_ia")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m scripts.estudio_sombra_ia", description=__doc__.split("\n\n")[0])
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
    from app.repositories.candles import get_candles
    from app.repositories.health import set_transaction_read_only
    from app.repositories.sombra_ia import leer_ciclos_con_consolidador, leer_prefiltro, leer_propuestas

    session = get_sessionmaker(get_engine(get_settings()))()
    try:
        set_transaction_read_only(session)
        # Las filas del repositorio y las entradas del dominio tienen los mismos campos;
        # la capa de datos no importa reglas de negocio, así que se convierten aquí.
        bases = construir_unidades_base(
            [CicloConConsolidador(**vars(f)) for f in leer_ciclos_con_consolidador(session)],
            [FilaPrefiltroS1(**vars(f)) for f in leer_prefiltro(session)],
            [PropuestaS1(**vars(f)) for f in leer_propuestas(session)],
        )

        def cargar_velas(simbolos):
            return {s: get_candles(session, s, "1h") for s in simbolos}

        resultado = correr(bases, cargar_velas)
    finally:
        session.rollback()
        session.close()

    print(f"Protocolo S1 v1 · huella {huella[:12]}…")
    if not resultado.evaluada:
        for l in formatear_conteo(resultado.conteo):
            print(l)
        return 0

    ahora = datetime.now(timezone.utc)
    nombre = nombre_de_salida(args.salida, ahora)
    titulo = (
        "EVALUACIÓN PRIMARIA — protocolo S1 v1"
        if nombre.startswith("evaluacion-primaria")
        else "ACTUALIZACIÓN (no sustituye a la evaluación primaria) — protocolo S1 v1"
    )
    for l in formatear_informe(resultado.informe, resultado.descartes, titulo=titulo):
        print(l)
    args.salida.mkdir(parents=True, exist_ok=True)
    ruta = args.salida / nombre
    ruta.write_text(
        json.dumps(informe_a_json(resultado.informe, resultado.descartes, huella=huella), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nGuardado en {ruta}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
