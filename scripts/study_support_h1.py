"""Estudio offline de la hipótesis H1: soporte + RSI 4h bajo -> recuperación.

Protocolo pre-registrado (parámetros, definiciones S1/S2, partición 70/30,
bootstrap y regla de decisión): `claude/protocolo-h1-soporte.md`. Este script
NO tiene opciones para cambiar esos parámetros a propósito: cambiarlos tras ver
resultados invalida el estudio. Solo lectura: no escribe en la base de datos,
no toca el score, ningún prompt ni ninguna regla viva.

Requisitos: velas 1h, 4h y 1d de la whitelist en la base (backfill de ~2 años)
y el historial REPARADO (sin velas vencidas sin cerrar:
`python -m scripts.repair_partial_candles --ejecutar`).

Uso:
    python -m scripts.study_support_h1
    python -m scripts.study_support_h1 --symbols BTCUSDT,ETHUSDT    # prueba rápida; el protocolo usa los 20

Código de salida: 0 = estudio completo; 1 = no se pudo correr (datos).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.config import get_settings
from app.data.candle_ingestion import velas_vencidas_sin_cerrar
from app.data.support_study import (
    BASE,
    DEFINICIONES,
    GRUPOS,
    Comparacion,
    Observacion,
    Particion,
    PAR_BARRERAS,
    analizar,
    observaciones_de_simbolo,
)
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.symbols import ALLOWED_SYMBOLS
from app.logging_config import configure_logging
from app.repositories.candles import get_candles


def _pct(valor) -> str:
    return "  n/d" if valor is None else f"{valor * 100:5.1f}%"


def _linea(c: Comparacion) -> str:
    i = c.intervalo
    ic = "n/d" if i.bajo is None else f"[{_pct(i.bajo)}, {_pct(i.alto)}]"
    dif = "n/d" if i.diferencia is None else f"{i.diferencia * 100:+5.1f} pp"
    return (
        f"  {c.grupo:22s} episodios: obj={c.episodios.objetivo:4d} stop={c.episodios.stop:4d} "
        f"tasa={_pct(c.episodios.tasa)} | base tasa={_pct(c.base.tasa)} | dif={dif} IC97,5%={ic} | "
        f"señales={c.senales.total:5d} amb={c.episodios.ambiguas:3d} sinres={c.episodios.sin_resolver:3d}"
    )


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", type=str, default=None, help="Subconjunto para pruebas (el protocolo usa los 20).")
    args = parser.parse_args(argv)
    symbols = tuple(s.strip() for s in args.symbols.split(",")) if args.symbols else ALLOWED_SYMBOLS
    for s in symbols:
        if s not in ALLOWED_SYMBOLS:
            print(f"ERROR: {s} no está en la lista autorizada.", file=sys.stderr)
            return 1

    session_factory = get_sessionmaker(get_engine(get_settings()))
    observaciones: list[Observacion] = []
    sin_datos: list[str] = []
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
            diarias = get_candles(session, symbol, "1d")
            velas_4h = get_candles(session, symbol, "4h")
            velas_1h = get_candles(session, symbol, "1h")
            obs = observaciones_de_simbolo(symbol, diarias, velas_4h, velas_1h)
            if not obs:
                sin_datos.append(symbol)
            observaciones.extend(obs)

    informe = analizar(observaciones)
    if informe is None:
        print("ERROR: no hay días elegibles (¿falta el backfill de ~2 años en 1h, 4h y 1d?).", file=sys.stderr)
        return 1

    print("ESTUDIO H1 — protocolo pre-registrado en claude/protocolo-h1-soporte.md")
    print(
        f"Barreras: stop -{PAR_BARRERAS.stop_pct * 100:g}% / objetivo +{PAR_BARRERAS.target_pct * 100:g}%. "
        f"Observaciones elegibles: {len(observaciones)} ({len({o.symbol for o in observaciones})} monedas). "
        f"Corte exploración/confirmación: {informe.corte}."
    )
    if sin_datos:
        print(f"Sin días elegibles: {', '.join(sin_datos)}")
    equilibrio = next(iter(informe.comparaciones.values())).equilibrio
    print(f"Punto de equilibrio con comisiones: tasa de objetivo > {_pct(equilibrio).strip()}\n")
    for particion in Particion:
        print(f"=== {particion.value.upper()} ===")
        base = next(c.base for (g, p), c in informe.comparaciones.items() if p == particion)
        print(f"  {BASE:22s} (línea base, todos los días) obj={base.objetivo} stop={base.stop} tasa={_pct(base.tasa)}")
        for grupo in GRUPOS:
            if grupo != BASE:
                print(_linea(informe.comparaciones[(grupo, particion)]))
        print()

    print("=== VEREDICTO (regla fija del protocolo, evaluada en CONFIRMACIÓN) ===")
    for d in DEFINICIONES:
        c = informe.comparaciones[(d, Particion.CONFIRMACION)]
        print(
            f"  {d}: muestra≥30 episodios resueltos={'sí' if c.muestra_suficiente else 'NO'} "
            f"({c.episodios.resueltas}), IC inferior>0={'sí' if c.cumple_intervalo else 'no'}, "
            f"supera equilibrio={'sí' if c.supera_equilibrio else 'no'} -> {'PASA' if c.pasa else 'no pasa'}"
        )
    print(f"\n  {informe.veredicto.value}" + (f"  (definición ganadora: {informe.ganadora})" if informe.ganadora else ""))
    print(
        "\nRecordatorio: aunque se respalde, esto NO cambia el score. El siguiente paso sería proponer un factor "
        "nuevo (versión nueva) y validarlo en paper con muestra nueva."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
