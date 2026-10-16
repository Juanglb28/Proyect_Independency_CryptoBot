"""Diagnósticos EXPLORATORIOS del backtest walk-forward (censura, factores,
rango dentro del día, ventanas 9/3/3).

Reglas y advertencias en `app/domain/rules/walk_forward_diagnostics.py`. Solo
lectura: no escribe en la base, no llama a la IA y NO cambia el score. Sirve para
entender por qué el score no discriminó; NO para reajustarlo.

Uso:
    python -m scripts.backtest_diagnosticos
    python -m scripts.backtest_diagnosticos --symbols BTCUSDT,ETHUSDT   # prueba rápida

Tarda varios minutos con los 20 símbolos (bootstrap de 1.000 iteraciones por medición).
Código de salida: 0 = informe completo; 1 = no se pudo correr (datos).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone

from app.config import get_settings
from app.data.backtest_study import observaciones_de_simbolo
from app.data.candle_ingestion import velas_vencidas_sin_cerrar
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.rules.walk_forward import InformeWalkForward, ObservacionBacktest, VentanasWalkForward, hasta_fecha
from app.domain.rules.walk_forward_diagnostics import InformeDiagnosticos, diagnosticos
from app.domain.symbols import ALLOWED_SYMBOLS
from app.logging_config import configure_logging
from app.repositories.candles import get_candles
from scripts.backtest_walk_forward import _linea, _pct


def _bloque(titulo: str, informe: InformeWalkForward) -> None:
    print(f"--- {titulo}")
    if informe.prueba_agregada is None:
        print("  (sin pliegues completos)")
        return
    for r in informe.pliegues:
        p = r.pliegue
        print(_linea(f"prueba {p.indice + 1}", r.prueba) + f" | {p.prueba_desde} → {p.prueba_hasta}")
    print(_linea("AGREGADA", informe.prueba_agregada))
    print(f"  veredicto: {informe.veredicto.value}")


def imprimir(d: InformeDiagnosticos) -> None:
    v = d.ventanas
    print("DIAGNÓSTICOS EXPLORATORIOS del backtest walk-forward (no reajustan nada)")
    print(
        f"Ventanas base: {v.entrenamiento_meses}/{v.prueba_meses}/{v.paso_meses} m. Solo se mide FUERA de muestra. "
        "IC 95 % sin corrección por comparaciones múltiples; intervalos optimistas (observaciones no independientes).\n"
    )

    print("=== 1. SESGO DE CENSURA (observaciones de prueba sin cierre, por tercil de score) ===")
    for c in d.censura:
        fraccion = "n/d" if c.fraccion is None else _pct(c.fraccion).strip()
        print(f"  tercil {c.nombre:5s}: {c.censuradas:4d} de {c.n:5d} sin cerrar ({fraccion})")
    _bloque("Repetido valorando las abiertas al último precio (en vez de excluirlas)", d.con_censuradas_marcadas)

    print("\n=== 2. POR FACTOR (el tercil se arma con el sub-score de cada factor) ===")
    for factor, informe in d.por_factor.items():
        _bloque(f"factor: {factor}", informe)

    print(f"\n=== 3. RANGO DENTRO DEL DÍA (percentil entre las monedas del mismo día; mínimo {d.minimo_monedas_por_dia} monedas) ===")
    _bloque("score reemplazado por su rango dentro del día", d.rango_dentro_del_dia)

    s = d.sensibilidad_ventanas.ventanas
    print(f"\n=== 4. SENSIBILIDAD DE VENTANAS ({s.entrenamiento_meses}/{s.prueba_meses}/{s.paso_meses} m) ===")
    _bloque("misma medición con otra partición", d.sensibilidad_ventanas)

    print(
        "\nRecordatorio: esto es EXPLORATORIO. Aunque un factor o una variante parezca funcionar, NO se reajusta el "
        "score con estos datos: sería otra estrategia, a validar solo hacia delante o con datos no vistos."
    )


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", type=str, default=None, help="Subconjunto para pruebas (por defecto, los autorizados).")
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

    session_factory = get_sessionmaker(get_engine(get_settings()))
    observaciones: list[ObservacionBacktest] = []
    with session_scope(session_factory) as session:
        vencidas = velas_vencidas_sin_cerrar(session, ahora=datetime.now(timezone.utc), symbols=symbols)
        if vencidas:
            print(
                f"ERROR: hay {len(vencidas)} vela(s) vencidas sin cerrar. "
                "Repara primero: python -m scripts.repair_partial_candles --ejecutar",
                file=sys.stderr,
            )
            return 1
        for symbol in symbols:
            observaciones.extend(
                observaciones_de_simbolo(symbol, get_candles(session, symbol, "1d"), get_candles(session, symbol, "1h"))
            )
    if not observaciones:
        print("ERROR: no hay observaciones (¿falta el backfill de ~2 años?).", file=sys.stderr)
        return 1
    if args.hasta_fecha is not None:
        observaciones = hasta_fecha(observaciones, args.hasta_fecha)
        if not observaciones:
            print(f"ERROR: no quedan observaciones anteriores a {args.hasta_fecha}.", file=sys.stderr)
            return 1
        print(f"(Solo decisiones anteriores a {args.hasta_fecha}.)")
    imprimir(diagnosticos(observaciones, VentanasWalkForward()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
