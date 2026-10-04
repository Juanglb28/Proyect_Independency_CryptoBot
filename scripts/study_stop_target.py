"""Estudio descriptivo de stop/objetivo con las velas históricas guardadas
(Paso 8, 2026-10-04). Solo lectura: no modifica la base de datos ni ninguna
regla de riesgo.

Para cada moneda y cada par (stop, objetivo), entra al cierre de una vela
cada `--paso` velas y mide qué barrera toca primero el precio. Es
incondicional (no usa el score): mide cuánto ruido de precio tiene cada
moneda frente a un stop dado. Ver `app/data/barrier_study.py` para lo que
este estudio es y lo que NO es.

Uso:
    python -m scripts.study_stop_target
    python -m scripts.study_stop_target --barreras 6:8,8:12 --timeframe 1h --paso 24
    python -m scripts.study_stop_target --symbols BTCUSDT,ETHUSDT

Defaults: temporalidad 1h (la más fina guardada: acerca el cálculo al
monitor de 60 s), una entrada por día (`--paso 24` velas), y los cuatro
cruces de stop {6%, 8%} con objetivo {8%, 12%}. Los porcentajes se escriben
en puntos (`6:8` = stop 6%, objetivo 8%).
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from app.data.barrier_study import (
    ParBarreras,
    ResumenBarreras,
    break_even_target_rate,
    evaluate_symbol,
    summarize,
    velas_a_dias,
)
from app.data.exchange_client import SUPPORTED_TIMEFRAMES
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.config import get_settings
from app.domain.symbols import ALLOWED_SYMBOLS
from app.logging_config import configure_logging
from app.repositories.candles import get_candles

_BARRERAS_POR_DEFECTO = "6:8,6:12,8:8,8:12"


def parse_barreras(texto: str) -> list[ParBarreras]:
    """`"6:8,8:12"` -> [stop 6% / objetivo 8%, stop 8% / objetivo 12%]."""
    pares: list[ParBarreras] = []
    for trozo in texto.split(","):
        stop_txt, _, target_txt = trozo.strip().partition(":")
        if not stop_txt or not target_txt:
            raise ValueError(f"Par de barreras inválido: {trozo!r}. Formato esperado: stop:objetivo, p. ej. 6:8")
        pares.append(
            ParBarreras(stop_pct=Decimal(stop_txt) / 100, target_pct=Decimal(target_txt) / 100)
        )
    return pares


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", type=str, default=None, help="Símbolos separados por coma (default: la whitelist).")
    parser.add_argument("--timeframe", type=str, default="1h", choices=SUPPORTED_TIMEFRAMES)
    parser.add_argument("--paso", type=int, default=24, help="Una entrada cada N velas (default 24 = diaria en 1h).")
    parser.add_argument("--barreras", type=str, default=_BARRERAS_POR_DEFECTO, help="Pares stop:objetivo en puntos %%.")
    return parser.parse_args(argv)


def _etiqueta(par: ParBarreras) -> str:
    return f"stop -{par.stop_pct * 100:g}% / obj +{par.target_pct * 100:g}%"


def _linea(resumen: ResumenBarreras, timeframe: str, titulo: str) -> str:
    tasa = "n/d" if resumen.tasa_target is None else f"{resumen.tasa_target * 100:5.1f}%"
    equilibrio = break_even_target_rate(resumen.par) * 100
    dias = (
        "n/d"
        if resumen.mediana_velas_hasta_resolver is None
        else f"{velas_a_dias(resumen.mediana_velas_hasta_resolver, timeframe):5.1f}"
    )
    return (
        f"{titulo:10s} {_etiqueta(resumen.par):26s} entradas={resumen.entradas:5d}  "
        f"obj-primero={resumen.target_primero:5d}  stop-primero={resumen.stop_primero:5d}  "
        f"ambiguas={resumen.ambiguas:3d}  sin-resolver={resumen.sin_resolver:4d}  "
        f"tasa-obj={tasa}  equilibrio={equilibrio:4.1f}%  mediana-dias={dias}"
    )


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = parse_args(argv)
    symbols = tuple(s.strip() for s in args.symbols.split(",")) if args.symbols else ALLOWED_SYMBOLS
    pares = parse_barreras(args.barreras)

    settings = get_settings()
    session_factory = get_sessionmaker(get_engine(settings))

    acumulado: dict[ParBarreras, list] = {par: [] for par in pares}
    sin_velas: list[str] = []

    print(
        f"Estudio de barreras — temporalidad {args.timeframe}, una entrada cada {args.paso} velas.\n"
        "Incondicional (sin score). 'tasa-obj' = entradas resueltas que tocaron el objetivo primero; "
        "'equilibrio' = tasa mínima para no perder tras comisiones (modelo simplificado).\n"
    )

    with session_scope(session_factory) as session:
        for symbol in symbols:
            velas = get_candles(session, symbol, args.timeframe)
            if len(velas) < 2:
                sin_velas.append(symbol)
                continue
            evaluaciones = evaluate_symbol(velas, pares, paso_velas=args.paso)
            for par in pares:
                acumulado[par].extend(evaluaciones[par])
                print(_linea(summarize(par, evaluaciones[par]), args.timeframe, symbol))
            print()

    print("=== TODAS LAS MONEDAS JUNTAS (entradas correlacionadas: orientativo) ===")
    for par in pares:
        print(_linea(summarize(par, acumulado[par]), args.timeframe, "TOTAL"))

    if sin_velas:
        print(f"\nSin velas {args.timeframe} suficientes (se saltaron): {', '.join(sin_velas)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
