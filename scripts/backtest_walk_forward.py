"""Backtest walk-forward del score CUANTITATIVO determinista (Fase C).

Responde: ¿las observaciones con score alto rinden mejor que las de score bajo,
y se mantiene fuera de la muestra? Reglas y límites en
`app/domain/rules/walk_forward.py`. Solo lectura: no escribe en la base de datos,
no llama a la IA (costo cero), no cambia el score ni ninguna regla viva.

Requisitos: velas 1h y 1d de la whitelist (backfill de ~2 años) y el historial
REPARADO (`python -m scripts.repair_partial_candles --ejecutar`).

Uso:
    python -m scripts.backtest_walk_forward
    python -m scripts.backtest_walk_forward --entrenamiento 9 --prueba 3 --paso 3
    python -m scripts.backtest_walk_forward --symbols BTCUSDT,ETHUSDT   # prueba rápida

Las ventanas por defecto (12/3/3 meses) son parámetros de análisis, no reglas de
riesgo. Si las cambias tras ver resultados, anótalo: es otra evaluación.

Código de salida: 0 = informe completo; 1 = no se pudo correr (datos).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

from app.config import get_settings
from app.data.backtest_study import observaciones_de_simbolo
from app.data.candle_ingestion import velas_vencidas_sin_cerrar
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.rules.walk_forward import (
    ENTRENAMIENTO_MESES_DEFECTO,
    PASO_MESES_DEFECTO,
    PRUEBA_MESES_DEFECTO,
    InformeWalkForward,
    ObservacionBacktest,
    ResultadoTramo,
    VentanasWalkForward,
    hasta_fecha,
    informe_walk_forward,
)
from app.domain.symbols import ALLOWED_SYMBOLS
from app.logging_config import configure_logging
from app.repositories.candles import get_candles


def _pct(valor: Decimal | None) -> str:
    return "   n/d" if valor is None else f"{valor * 100:+6.2f}%"


def _linea(etiqueta: str, t: ResultadoTramo) -> str:
    if t.discriminacion is None:
        return f"  {etiqueta:14s} n={t.n:5d} (sin datos suficientes para terciles) censuradas={t.censuradas} purgadas={t.purgadas}"
    return (
        f"  {etiqueta:14s} n={t.n:5d} [{t.estado.value}] alto={_pct(t.expectancy_alta)} bajo={_pct(t.expectancy_baja)} "
        f"dif={_pct(t.discriminacion)} IC95=[{_pct(t.ci_inferior).strip()}, {_pct(t.ci_superior).strip()}] "
        f"discrimina={'SI' if t.discrimina else 'no'} | censuradas={t.censuradas} purgadas={t.purgadas}"
    )


def imprimir(informe: InformeWalkForward, n_obs: int, n_simbolos: int) -> None:
    v = informe.ventanas
    print(f"BACKTEST WALK-FORWARD del score cuantitativo (versión {informe.version})")
    print(
        f"Ventanas: entrenamiento {v.entrenamiento_meses} m / prueba {v.prueba_meses} m / paso {v.paso_meses} m. "
        f"Observaciones: {n_obs} ({n_simbolos} monedas), {informe.primer_dia} a {informe.ultimo_dia}."
    )
    print("Retornos netos por entrada (stop -8 %, cierre 50 % en +12 %, trailing 6 %, comisión 0,1 % por lado). Terciles por score.\n")
    for r in informe.pliegues:
        p = r.pliegue
        print(f"Pliegue {p.indice + 1}: entrena {p.entrena_desde} → {p.entrena_hasta} | prueba {p.prueba_desde} → {p.prueba_hasta}")
        print(_linea("entrenamiento", r.entrenamiento))
        print(_linea("PRUEBA", r.prueba))
    if informe.prueba_agregada is not None:
        print("\n=== PRUEBA AGREGADA (decide el veredicto) ===")
        print(_linea("todas", informe.prueba_agregada))
        print(f"  Pruebas con diferencia positiva: {informe.pliegues_con_diferencia_positiva} de {len(informe.pliegues)}")
    print(f"\nVEREDICTO: {informe.veredicto.value}")
    for nota in informe.notas:
        print(f"  - {nota}")


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", type=str, default=None, help="Subconjunto para pruebas (por defecto, los autorizados).")
    parser.add_argument("--entrenamiento", type=int, default=ENTRENAMIENTO_MESES_DEFECTO, help="Meses de entrenamiento.")
    parser.add_argument("--prueba", type=int, default=PRUEBA_MESES_DEFECTO, help="Meses de prueba.")
    parser.add_argument("--paso", type=int, default=PASO_MESES_DEFECTO, help="Meses que avanza cada pliegue.")
    parser.add_argument(
        "--hasta-fecha",
        type=date.fromisoformat,
        default=None,
        help="Evaluar solo decisiones ANTERIORES a esta fecha (AAAA-MM-DD), p. ej. una época por separado.",
    )
    args = parser.parse_args(argv)
    symbols = tuple(s.strip() for s in args.symbols.split(",")) if args.symbols else ALLOWED_SYMBOLS
    for s in symbols:
        if s not in ALLOWED_SYMBOLS:
            print(f"ERROR: {s} no está en la lista autorizada.", file=sys.stderr)
            return 1
    try:
        ventanas = VentanasWalkForward(args.entrenamiento, args.prueba, args.paso)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    session_factory = get_sessionmaker(get_engine(get_settings()))
    observaciones: list[ObservacionBacktest] = []
    with session_scope(session_factory) as session:
        vencidas = velas_vencidas_sin_cerrar(session, ahora=datetime.now(timezone.utc), symbols=symbols)
        if vencidas:
            print(
                f"ERROR: hay {len(vencidas)} vela(s) vencidas sin cerrar (datos parciales antiguos). "
                "Repara primero: python -m scripts.repair_partial_candles --ejecutar",
                file=sys.stderr,
            )
            return 1
        for symbol in symbols:
            observaciones.extend(
                observaciones_de_simbolo(symbol, get_candles(session, symbol, "1d"), get_candles(session, symbol, "1h"))
            )

    if not observaciones:
        print("ERROR: no hay observaciones (¿falta el backfill de ~2 años en 1h y 1d?).", file=sys.stderr)
        return 1
    if args.hasta_fecha is not None:
        observaciones = hasta_fecha(observaciones, args.hasta_fecha)
        if not observaciones:
            print(f"ERROR: no quedan observaciones anteriores a {args.hasta_fecha}.", file=sys.stderr)
            return 1
        print(f"(Solo decisiones anteriores a {args.hasta_fecha}.)")
    imprimir(informe_walk_forward(observaciones, ventanas), len(observaciones), len({o.symbol for o in observaciones}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
