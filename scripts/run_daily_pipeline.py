"""Encadena ingesta incremental + indicadores + ciclo diario.

Por qué existe: se detectó en producción (2026-10-01/02) que el ciclo
diario (`scripts/run_daily_cycle.py`) llevaba corriendo dos días seguidos
sobre velas con más de 50 horas de antigüedad — el Auditor lo señalaba en
cada corrida ("Antigüedad crítica"), bajando `calidad_datos` y, con eso, el
puntaje ponderado final. La causa: nada actualizaba `velas`/`indicadores`
automáticamente; `run_daily_cycle.py` asume (correctamente, por diseño del
Paso 7 — ver `app/domain/enums.py::EstadoCiclo`) que esos datos ya están al
día, porque la ingesta y el cálculo de indicadores son pasos separados.

Este script no reabre esa decisión: `daily_cycle.py` sigue sin ingerir ni
calcular nada. Solo encadena, en orden, los tres comandos que ya existían
por separado y que un humano tendría que acordarse de correr a mano todos
los días:

1. `scripts.backfill_candles --days 5` — ventana chica y barata (no los 2
   años completos de una carga inicial), suficiente para cubrir incluso
   un día en el que la tarea programada no se disparó. Idempotente: no
   duplica nada si ya hay datos de ese rango.
2. `scripts.calculate_indicators` — recalcula sobre las velas ya frescas.
   También idempotente.
3. `scripts.run_daily_cycle` — el ciclo real, sin cambios.

Cada paso corre como un subproceso separado (la misma invocación que ya
tenía como script independiente) para no duplicar su parseo de argumentos
ni su manejo de sesión — si alguno falla (código de salida distinto de
cero), el pipeline se detiene ahí y no corre los siguientes, en vez de
analizar con datos a medio actualizar.

Uso:
    python -m scripts.run_daily_pipeline
"""

from __future__ import annotations

import subprocess
import sys

_INGEST_DAYS = "5"


def main(argv: list[str] | None = None) -> int:
    del argv  # Sin argumentos propios por ahora — cada paso usa sus defaults.

    steps: list[list[str]] = [
        [sys.executable, "-m", "scripts.backfill_candles", "--days", _INGEST_DAYS],
        [sys.executable, "-m", "scripts.calculate_indicators"],
        [sys.executable, "-m", "scripts.run_daily_cycle"],
    ]

    for step in steps:
        print(f"\n=== Ejecutando: {' '.join(step)} ===", flush=True)
        result = subprocess.run(step)
        if result.returncode != 0:
            print(
                f"\nERROR: '{' '.join(step)}' terminó con código {result.returncode}. "
                "Deteniendo el pipeline — no se corre el ciclo con datos a medio actualizar.",
                file=sys.stderr,
            )
            return result.returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
