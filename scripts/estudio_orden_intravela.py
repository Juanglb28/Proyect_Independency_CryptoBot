"""Sensibilidad al ORDEN INTRAVELA de la simulación de salida (2026-10-17).

Pregunta: la simulación usa velas de 1h y no sabe si, dentro de una vela, el máximo
o el mínimo ocurrió primero. Los resultados publicados suponen «máximo primero».
¿Cuánto cambian el retorno medio y la discriminación del score si se supone el
orden inverso («mínimo primero»)? La realidad cae entre ambas lecturas.

Es un análisis NUEVO y declarado como tal: solo lectura (no escribe en la base de
datos, no llama a la IA), no cambia el score, el motor paper ni ningún resultado
previo. Usa las mismas observaciones del backtest walk-forward, con la misma
semilla del bootstrap; lo único que cambia es la suposición intravela.

Uso:
    python -m scripts.estudio_orden_intravela
    python -m scripts.estudio_orden_intravela --symbols BTCUSDT,ETHUSDT   # prueba rápida
    python -m scripts.estudio_orden_intravela --desde 2025-03-19          # solo la época no vista

Código de salida: 0 = informe completo; 1 = no se pudo correr (datos).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

from app.config import get_settings
from app.data.backtest_study import observaciones_de_simbolo
from app.data.candle_ingestion import velas_vencidas_que_invalidan_un_estudio
from app.data.exit_simulation import MAXIMO_PRIMERO, MINIMO_PRIMERO
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.rules.intravela import ComparacionIntravela, comparar_ordenes
from app.domain.rules.walk_forward import ObservacionBacktest, ResultadoTramo, evaluar_tramo, informe_walk_forward
from app.domain.symbols import ALLOWED_SYMBOLS
from app.logging_config import configure_logging
from app.repositories.candles import get_candles


def _pct(valor: Decimal | None) -> str:
    return "   n/d" if valor is None else f"{valor * 100:+7.3f}%"


def _linea(etiqueta: str, t: ResultadoTramo) -> str:
    if t.discriminacion is None:
        return f"  {etiqueta:18s} n={t.n:5d} (sin datos suficientes para terciles) censuradas={t.censuradas}"
    return (
        f"  {etiqueta:18s} n={t.n:5d} alto={_pct(t.expectancy_alta)} bajo={_pct(t.expectancy_baja)} "
        f"dif={_pct(t.discriminacion)} IC95=[{_pct(t.ci_inferior).strip()}, {_pct(t.ci_superior).strip()}] "
        f"discrimina={'SI' if t.discrimina else 'no'} | censuradas={t.censuradas}"
    )


def imprimir(c: ComparacionIntravela, base: list[ObservacionBacktest], inversa: list[ObservacionBacktest]) -> None:
    print("SENSIBILIDAD AL ORDEN INTRAVELA (análisis nuevo, solo lectura)")
    print(f"Observaciones: {c.n_total}. Resueltas en AMBAS lecturas: {c.n_resueltas_ambas}.")
    print(
        f"Censuradas: {c.censuradas_base} con «{MAXIMO_PRIMERO}» y {c.censuradas_inversa} con «{MINIMO_PRIMERO}» "
        f"(la inversa puede resolver alguna que la base deja abierta)."
    )
    print("\n--- Efecto sobre el retorno neto por entrada (observaciones resueltas en ambas) ---")
    print(f"  Media con «{MAXIMO_PRIMERO}» (publicada): {_pct(c.media_base)}")
    print(f"  Media con «{MINIMO_PRIMERO}» (inversa):   {_pct(c.media_inversa)}")
    print(f"  Diferencia media (inversa - base):        {_pct(c.diferencia_media)}")
    print(
        f"  Entradas que cambian: {c.n_cambian} ({c.pct_cambian * 100:.2f}% de las resueltas); "
        f"la inversa rinde MÁS en {c.n_inversa_mejor}, MENOS en {c.n_inversa_peor}."
    )
    print(
        f"  Cuando cambian: diferencia media {_pct(c.diferencia_media_si_cambian)}, "
        f"mejor caso {_pct(c.mayor_ganancia_inversa)}, peor caso {_pct(c.mayor_perdida_inversa)}."
    )
    print("\n--- Efecto sobre la discriminación del score (terciles, bootstrap 95 %, misma semilla) ---")
    print(_linea(f"{MAXIMO_PRIMERO}", evaluar_tramo(base)))
    print(_linea(f"{MINIMO_PRIMERO}", evaluar_tramo(inversa)))
    vb = informe_walk_forward(base, con_entrenamiento=False)
    vi = informe_walk_forward(inversa, con_entrenamiento=False)
    print(f"\nVeredicto walk-forward: {vb.veredicto.value} con «{MAXIMO_PRIMERO}» | {vi.veredicto.value} con «{MINIMO_PRIMERO}».")
    print(
        "\nNota: ninguna de las dos lecturas es «la verdadera». Si el veredicto y el signo de la diferencia no cambian, "
        "el orden intravela no es un riesgo para las conclusiones. Resolverlo del todo exigiría velas de 1 minuto "
        "solo en las pocas velas ambiguas (análisis aparte)."
    )


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", type=str, default=None, help="Subconjunto para pruebas (por defecto, los autorizados).")
    parser.add_argument(
        "--desde", type=date.fromisoformat, default=None, help="Solo decisiones desde esta fecha incluida (AAAA-MM-DD)."
    )
    args = parser.parse_args(argv)
    symbols = tuple(s.strip() for s in args.symbols.split(",")) if args.symbols else ALLOWED_SYMBOLS
    for s in symbols:
        if s not in ALLOWED_SYMBOLS:
            print(f"ERROR: {s} no está en la lista autorizada.", file=sys.stderr)
            return 1

    base: list[ObservacionBacktest] = []
    inversa: list[ObservacionBacktest] = []
    session_factory = get_sessionmaker(get_engine(get_settings()))
    with session_scope(session_factory) as session:
        vencidas = velas_vencidas_que_invalidan_un_estudio(session, ahora=datetime.now(timezone.utc), symbols=symbols)
        if vencidas:
            print(
                f"ERROR: hay {len(vencidas)} vela(s) vencidas sin cerrar (datos parciales antiguos). "
                "Repara primero: python -m scripts.repair_partial_candles --ejecutar",
                file=sys.stderr,
            )
            return 1
        for symbol in symbols:
            diarias, horarias = get_candles(session, symbol, "1d"), get_candles(session, symbol, "1h")
            base.extend(observaciones_de_simbolo(symbol, diarias, horarias, orden_intravela=MAXIMO_PRIMERO))
            inversa.extend(observaciones_de_simbolo(symbol, diarias, horarias, orden_intravela=MINIMO_PRIMERO))

    if args.desde is not None:
        base = [o for o in base if o.dia >= args.desde]
        inversa = [o for o in inversa if o.dia >= args.desde]
        print(f"(Solo decisiones desde {args.desde}.)")
    if not base:
        print("ERROR: no hay observaciones (¿falta el backfill de ~2 años en 1h y 1d?).", file=sys.stderr)
        return 1
    imprimir(comparar_ordenes(base, inversa), base, inversa)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
