"""Corre el pre-filtro determinista sobre los 20 símbolos reales (sección
4.2/6.1 del plan, Paso 6.1) y muestra el resultado.

De solo lectura: no persiste nada (las tablas `factores`/`propuestas`
pertenecen a un paso posterior). Usa siempre la última vela diaria y los
indicadores diarios ya guardados de cada símbolo (Paso 4) — si no se
corrió `calculate_indicators`/`backfill_candles` recientemente, el
resultado refleja datos desactualizados, no un error de este script.

Uso:
    python -m scripts.run_prefilter
"""

from __future__ import annotations

from app.config import get_settings
from app.data.prefilter_pipeline import run_prefilter
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.logging_config import configure_logging


def main() -> int:
    configure_logging()
    settings = get_settings()
    engine = get_engine(settings)
    session_factory = get_sessionmaker(engine)

    with session_scope(session_factory) as session:
        result = run_prefilter(session)

    print(f"Símbolos evaluados con datos completos: {len(result.scores)}")
    if result.symbols_sin_datos:
        print(f"Símbolos sin datos suficientes ({len(result.symbols_sin_datos)}):")
        for symbol in result.symbols_sin_datos:
            print(f"  - {symbol}")

    print("\nPuntajes (orden descendente):")
    for score in sorted(result.scores, key=lambda s: (-s.score_final, s.symbol)):
        print(
            f"  {score.symbol:<10} score_final={score.score_final:>6.2f}  "
            f"tendencia={score.tendencia_pct:>7.2f}%->{score.score_tendencia:>6.2f}  "
            f"volumen={score.volumen_relativo:>5.2f}x->{score.score_volumen:>6.2f}  "
            f"dist_90d={score.distancia_max_90d_pct:>7.2f}%->{score.score_distancia:>6.2f}"
        )

    print(f"\nCandidatos seleccionados ({len(result.candidates)}):")
    if not result.candidates:
        print("  (ninguno — día de mercado débil, ciclo terminaría aquí sin llamar a la IA)")
    for score in result.candidates:
        print(f"  - {score.symbol} (score_final={score.score_final:.2f})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
