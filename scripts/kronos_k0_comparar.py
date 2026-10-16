"""Compara dos informes de K0 (mismas monedas y fecha, distinto muestreo).

    docker compose --profile kronos run --rm kronos python -m scripts.kronos_k0_comparar \
        data/kronos/k0-A.json data/kronos/k0-B.json

Solo lee los dos JSON; no usa red, base ni el modelo.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.experiments.kronos.comparar import comparar_corridas, formatear_comparacion


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("Uso: python -m scripts.kronos_k0_comparar <informe_A.json> <informe_B.json>", file=sys.stderr)
        return 2
    a, b = (json.loads(Path(x).read_text(encoding="utf-8")) for x in argv)
    for linea in formatear_comparacion(comparar_corridas(a, b)):
        print(linea)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
