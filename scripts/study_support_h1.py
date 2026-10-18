"""Estudio offline de la hipótesis H1: soporte + RSI 4h bajo -> recuperación.

Protocolo pre-registrado (parámetros, definiciones S1/S2, partición 70/30,
bootstrap y regla de decisión): `docs/protocolo-h1-soporte.md`. Este script
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
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.data.candle_ingestion import velas_vencidas_que_invalidan_un_estudio
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


def _pp(valor) -> str:
    return "   n/d" if valor is None else f"{valor * 100:+6.1f}"


def _ic(e) -> str:
    return "n/d" if e.bajo is None else f"[{_pp(e.bajo).strip()}, {_pp(e.alto).strip()}]"


def _linea(c: Comparacion) -> list[str]:
    ep = c.episodios
    tb, ta = c.tasa_bruta, c.tasa_ajustada
    rb, ra = c.retorno_bruto, c.retorno_ajustado
    return [
        f"  {c.grupo:22s} episodios: obj={ep.objetivo:4d} stop={ep.stop:4d} amb={ep.ambiguas:3d} "
        f"sinres={ep.sin_resolver:3d} | tasa={_pct(ep.tasa)} | señales={c.senales.total:5d} | purgados={c.purgadas_episodios}",
        f"  {'':22s} vs base BRUTA    ({_pct(tb.base)}): dif={_pp(tb.diferencia)} pp  IC97,5%=[pp] {_ic(tb)}",
        f"  {'':22s} vs base AJUSTADA ({_pct(ta.base)}, {ta.estratos or 0} estratos): "
        f"dif={_pp(ta.diferencia)} pp  IC97,5%=[pp] {_ic(ta)}",
        f"  {'':22s} secundario (salida real simulada, neto): retorno medio={_pct(rb.cond)} vs base {_pct(rb.base)}"
        f" | dif bruta={_pp(rb.diferencia)} pp {_ic(rb)} | dif ajustada={_pp(ra.diferencia)} pp {_ic(ra)}",
    ]


def _sha256_protocolo() -> str | None:
    ruta = Path(__file__).resolve().parent.parent / "docs" / "protocolo-h1-soporte.md"
    if not ruta.exists():
        return None
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


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
        vencidas = velas_vencidas_que_invalidan_un_estudio(session, ahora=datetime.now(timezone.utc), symbols=symbols)
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

    print("ESTUDIO H1 — protocolo pre-registrado en docs/protocolo-h1-soporte.md (v2)")
    huella = _sha256_protocolo()
    print(f"SHA-256 del protocolo: {huella or 'NO ENCONTRADO (el estudio no es verificable sin el protocolo)'}")
    print(
        f"Barreras: stop -{PAR_BARRERAS.stop_pct * 100:g}% / objetivo +{PAR_BARRERAS.target_pct * 100:g}%. "
        f"Observaciones elegibles: {len(observaciones)} ({len({o.symbol for o in observaciones})} monedas). "
        f"Corte exploración/confirmación: {informe.corte}."
    )
    if sin_datos:
        print(f"Sin días elegibles: {', '.join(sin_datos)}")
    equilibrio = next(iter(informe.comparaciones.values())).equilibrio
    print(f"Punto de equilibrio con comisiones: tasa de objetivo > {_pct(equilibrio).strip()}")
    print("Diferencias e intervalos en puntos porcentuales (pp). 'purgados' = episodios de exploración cuyo desenlace")
    print("cae en/después del corte o no se resolvió (excluidos para no filtrar información de la confirmación).\n")
    for particion in Particion:
        print(f"=== {particion.value.upper()} ===")
        c0 = next(c for (g, p), c in informe.comparaciones.items() if p == particion)
        base = c0.base
        print(
            f"  {BASE:22s} (línea base, todos los días) obj={base.objetivo} stop={base.stop} "
            f"tasa={_pct(base.tasa)} | purgados={c0.purgadas_base}"
        )
        for grupo in GRUPOS:
            if grupo != BASE:
                for linea in _linea(informe.comparaciones[(grupo, particion)]):
                    print(linea)
        print()

    print("=== VEREDICTO (regla fija del protocolo, evaluada por definición en CONFIRMACIÓN) ===")
    for d in DEFINICIONES:
        c = informe.comparaciones[(d, Particion.CONFIRMACION)]
        print(
            f"  {d}: {informe.estados[d].value} | episodios resueltos={c.episodios.resueltas} (mín. 30) | "
            f"IC inferior>0 vs bruta={'sí' if c.tasa_bruta.lim_inferior_positivo else 'no'}, "
            f"vs ajustada={'sí' if c.tasa_ajustada.lim_inferior_positivo else 'no'} | "
            f"supera equilibrio={'sí' if c.supera_equilibrio else 'no'}"
        )
    print(f"\n  {informe.veredicto.value}" + (f"  (definición ganadora: {informe.ganadora})" if informe.ganadora else ""))
    print(
        "\nRecordatorio: el estudio es EXPLORATORIO. Aunque se respalde, esto NO cambia el score: solo justifica "
        "continuar la prueba en paper con datos nuevos (un factor nuevo, versión nueva, validado hacia delante)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
