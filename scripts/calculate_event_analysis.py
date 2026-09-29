"""Cálculo de `analisis_eventos` para todo el catálogo (sección 15.10 del
plan, Paso 5).

Calcula y guarda, para cada evento ya cargado con `scripts/seed_events.py`,
el comportamiento del precio en las ventanas fijadas (-7, -1, +1, +3, +7,
+30 días) para cada símbolo de su `scope` — a partir de las velas diarias
YA guardadas en la base de datos (no descarga nada de Binance).

Uso:
    python -m scripts.calculate_event_analysis

Es idempotente (misma restricción única que `indicadores`): correr esto dos
veces no duplica ningún valor. Un símbolo sin velas diarias guardadas, o un
evento más antiguo que el historial de velas, se reporta como "sin velas"
en vez de fallar (sección P3 del plan).
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.data.event_pipeline import calculate_event_analysis_for_all
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    configure_logging()

    settings = get_settings()
    engine = get_engine(settings)
    session_factory = get_sessionmaker(engine)

    with session_scope(session_factory) as session:
        results = calculate_event_analysis_for_all(session)

    if not results:
        print("No hay eventos cargados todavía. Correr primero: python -m scripts.seed_events")
        return 0

    total_inserted = 0
    for result in results:
        sin_velas = f"  sin_velas={','.join(result.symbols_sin_velas)}" if result.symbols_sin_velas else ""
        print(
            f"{result.external_id:30s}  símbolos={result.symbols_evaluated:3d}  "
            f"calculados={result.calculated:5d}  insertados={result.inserted:5d}{sin_velas}"
        )
        total_inserted += result.inserted

    print(f"\nOK: {total_inserted} filas de analisis_eventos nuevas insertadas en total.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
