"""Resolución de las velas ambiguas con velas de 1 MINUTO (2026-10-17).

Continuación de `scripts.estudio_orden_intravela`: allí se midió cuánto cambia el
resultado según se suponga «máximo primero» o «mínimo primero» dentro de una vela de
1h. Aquí se elimina la suposición SOLO donde importa: en las velas de 1h cuyo
desenlace depende del orden intravela (`app.data.exit_simulation.clase_ambiguedad`),
se descargan sus 60 velas de 1 minuto y se recorren con las mismas reglas.

Clases de vela ambigua:
- `stop_y_objetivo`: antes del cierre parcial, la vela toca el stop y el objetivo
  (la regla de 1h cuenta STOP).
- `parcial_y_trailing`: la vela toca el objetivo y su rango bastaría para tocar también
  el trailing (la regla de 1h no evalúa el resto de esa vela).
- `orden_trailing`: tras el cierre parcial, el máximo sube y el mínimo toca el trailing
  nuevo (el caso medido en el estudio anterior).

Es un análisis NUEVO y declarado como tal: solo lectura (no escribe en la base, no
llama a la IA), no cambia el score, el motor paper ni ningún resultado publicado. Las
velas de 1 minuto son datos públicos de Binance; se validan contra la vela de 1h (deben
agregarla exactamente) y se guardan en una caché de archivos (`data/cache_velas_1m/`,
fuera de Git). Una vela sin datos válidos NO se rellena: usa la regla de 1h y se cuenta.

Uso:
    python -m scripts.estudio_velas_1m --solo-contar      # cuántas velas habría que bajar; sin red
    python -m scripts.estudio_velas_1m                    # descarga (con caché) e informe completo
    python -m scripts.estudio_velas_1m --symbols BTCUSDT,ETHUSDT --desde 2025-03-19
    python -m scripts.estudio_velas_1m --orden-dentro-del-minuto minimo_primero   # acota el orden dentro de un minuto

Código de salida: 0 = informe completo con todas las velas ambiguas resueltas;
1 = no se pudo correr (datos); 2 = informe con velas SIN resolver (vuelve a correr:
la caché conserva lo ya descargado).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.config import get_settings
from app.data.backtest_study import observaciones_de_simbolo
from app.data.candle_ingestion import velas_vencidas_que_invalidan_un_estudio
from app.data.exit_simulation import MAXIMO_PRIMERO, MINIMO_PRIMERO
from app.data.minute_candles import DIRECTORIO_CACHE_POR_DEFECTO, ResolvedorMinutos
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.rules.intravela import ComparacionIntravela, UbicacionResuelta, comparar_ordenes, ubicar_resuelta
from app.domain.rules.walk_forward import ObservacionBacktest, evaluar_tramo, informe_walk_forward
from app.domain.symbols import ALLOWED_SYMBOLS
from app.logging_config import configure_logging
from app.repositories.candles import get_candles
from scripts.estudio_orden_intravela import _linea, _pct

_SEGUNDOS_POR_VELA = 0.25  # estimación gruesa: límite de peticiones de ccxt + red


def imprimir_cobertura(r: ResolvedorMinutos) -> None:
    unicas = r.velas_ambiguas_unicas()
    print("COBERTURA")
    print(f"  Velas de 1h ambiguas consultadas (únicas): {len(unicas)}")
    for clase, n in r.por_clase().items():
        print(f"    {clase:20s} {n}")
    print(f"  Resueltas con velas de 1 minuto: {len(unicas) - len(r.no_resueltas)}  "
          f"(descargadas ahora: {r.descargadas}, desde caché: {r.desde_cache})")
    print(f"  SIN resolver (usan la regla de 1h): {len(r.no_resueltas)}")
    for motivo, n in r.motivos_no_resueltas().items():
        print(f"    {motivo:32s} {n}")
    print(f"  Mayor rango de un solo minuto usado: {_pct(r.max_rango_minuto)}")
    n = len(r.minutos_ambiguos)
    if n == 0:
        print("  Ningún minuto usado tiene rango suficiente (>= 6 % de su máximo) para ser ambiguo por sí solo: "
              "la suposición dentro del minuto no puede cambiar nada.")
    else:
        horas = len({(s, m.replace(minute=0)) for s, m in r.minutos_ambiguos})
        print(f"  ATENCIÓN: {n} minuto(s) usado(s) en {horas} hora(s) tienen rango suficiente para ser ambiguos por sí "
              "solos; dentro de ellos se supone el orden indicado en --orden-dentro-del-minuto "
              "(por defecto «máximo primero»). Los más grandes:")
        for (s, m), rango in sorted(r.minutos_ambiguos.items(), key=lambda kv: kv[1], reverse=True)[:8]:
            print(f"    {s:10s} {m:%Y-%m-%d %H:%M} UTC  rango {_pct(rango).strip()}")


def _ubicacion(u: UbicacionResuelta) -> None:
    if u.n == 0:
        print("  (sin observaciones resueltas en las tres lecturas)")
        return
    print(f"  Observaciones resueltas en las tres lecturas: {u.n}")
    print(f"  Las tres lecturas dan lo mismo:      {u.n_sin_cambio}")
    print(f"  Real == «{MAXIMO_PRIMERO}» (≠ inversa):  {u.n_igual_base}")
    print(f"  Real == «{MINIMO_PRIMERO}» (≠ base):    {u.n_igual_inversa}")
    print(f"  Real ENTRE las dos lecturas:         {u.n_entre}")
    print(f"  Real FUERA de ambas (distinto de las dos): {u.n_fuera}")
    print(f"  Retorno medio: base {_pct(u.media_base).strip()} | inversa {_pct(u.media_inversa).strip()} | "
          f"REAL (1 min) {_pct(u.media_resuelta).strip()}")


def _efecto(titulo: str, c: ComparacionIntravela) -> None:
    print(f"  {titulo}: diferencia media {_pct(c.diferencia_media).strip()}; cambian {c.n_cambian} "
          f"({c.pct_cambian * 100:.2f}%), la real rinde MÁS en {c.n_inversa_mejor} y MENOS en {c.n_inversa_peor}; "
          f"mejor caso {_pct(c.mayor_ganancia_inversa).strip()}, peor caso {_pct(c.mayor_perdida_inversa).strip()}.")


def imprimir(
    r: ResolvedorMinutos,
    base: list[ObservacionBacktest],
    inversa: list[ObservacionBacktest],
    resuelta: list[ObservacionBacktest],
) -> None:
    print("RESOLUCIÓN DE LAS VELAS AMBIGUAS CON VELAS DE 1 MINUTO (análisis nuevo, solo lectura)")
    print(f"Observaciones: {len(base)}.\n")
    imprimir_cobertura(r)
    print("\n--- Dónde cae el desenlace real respecto de las dos lecturas de 1h ---")
    _ubicacion(ubicar_resuelta(base, inversa, resuelta))
    print("\n--- Efecto de la resolución sobre el retorno neto por entrada ---")
    _efecto(f"Real (1 min) − «{MAXIMO_PRIMERO}» (la publicada)", comparar_ordenes(base, resuelta))
    _efecto(f"Real (1 min) − «{MINIMO_PRIMERO}»", comparar_ordenes(inversa, resuelta))
    print("\n--- Discriminación del score (terciles, bootstrap 95 %, misma semilla) ---")
    print(_linea(MAXIMO_PRIMERO, evaluar_tramo(base)))
    print(_linea(MINIMO_PRIMERO, evaluar_tramo(inversa)))
    print(_linea("REAL (1 minuto)", evaluar_tramo(resuelta)))
    vb = informe_walk_forward(base, con_entrenamiento=False).veredicto
    vi = informe_walk_forward(inversa, con_entrenamiento=False).veredicto
    vr = informe_walk_forward(resuelta, con_entrenamiento=False).veredicto
    print(f"\nVeredicto walk-forward: {vb.value} con «{MAXIMO_PRIMERO}» | {vi.value} con «{MINIMO_PRIMERO}» | "
          f"{vr.value} REAL (1 minuto).")
    if vr == vb:
        print("El veredicto REAL coincide con el publicado: la suposición intravela no afecta la conclusión.")
    else:
        print("ATENCIÓN: el veredicto REAL difiere del publicado; documentar antes de concluir nada.")
    print(
        "\nNota: el resultado real incluye también el resto de la hora en que se toca el objetivo "
        "(la regla de 1h no lo evalúa). Las velas ambiguas sin datos válidos conservan la regla de 1h."
    )


def imprimir_solo_contar(r: ResolvedorMinutos, base: list[ObservacionBacktest]) -> None:
    unicas = r.velas_ambiguas_unicas()
    pendientes = [clave for clave, m in r.no_resueltas.items() if m == "pendiente_de_descargar"]
    print("CONTEO (sin descargar nada)")
    print(f"  Observaciones: {len(base)}")
    print(f"  Velas de 1h ambiguas en los recorridos publicados (únicas): {len(unicas)}")
    for clase, n in r.por_clase().items():
        print(f"    {clase:20s} {n}")
    print(f"  Ya en caché: {len(unicas) - len(pendientes)}   Por descargar: {len(pendientes)} "
          f"(una petición pública de 60 velas cada una; ~{len(pendientes) * _SEGUNDOS_POR_VELA / 60:.0f} min)")
    print("  Cota inferior: al resolver una vela puede cambiar el camino y aparecer alguna más.")


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", type=str, default=None, help="Subconjunto para pruebas (por defecto, los autorizados).")
    parser.add_argument("--desde", type=date.fromisoformat, default=None, help="Solo decisiones desde esta fecha (AAAA-MM-DD).")
    parser.add_argument(
        "--orden-dentro-del-minuto", choices=(MAXIMO_PRIMERO, MINIMO_PRIMERO), default=MAXIMO_PRIMERO,
        help="Orden supuesto DENTRO de un minuto (solo importa en minutos de rango >= 6 %%). Correr con ambos acota el efecto.",
    )
    parser.add_argument("--solo-contar", action="store_true", help="Cuenta las velas por descargar; no usa la red.")
    parser.add_argument("--cache", type=Path, default=DIRECTORIO_CACHE_POR_DEFECTO, help="Directorio de la caché de 1 minuto.")
    args = parser.parse_args(argv)
    symbols = tuple(s.strip() for s in args.symbols.split(",")) if args.symbols else ALLOWED_SYMBOLS
    for s in symbols:
        if s not in ALLOWED_SYMBOLS:
            print(f"ERROR: {s} no está en la lista autorizada.", file=sys.stderr)
            return 1

    resolvedor = ResolvedorMinutos(directorio=args.cache, descargar=not args.solo_contar)
    base: list[ObservacionBacktest] = []
    inversa: list[ObservacionBacktest] = []
    resuelta: list[ObservacionBacktest] = []
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
            if args.solo_contar:
                # Solo el recorrido publicado, con el resolvedor en modo conteo (no descarga).
                observaciones_de_simbolo(symbol, diarias, horarias, resolvedor=resolvedor)
                continue
            inversa.extend(observaciones_de_simbolo(symbol, diarias, horarias, orden_intravela=MINIMO_PRIMERO))
            resuelta.extend(
                observaciones_de_simbolo(
                    symbol, diarias, horarias, orden_intravela=args.orden_dentro_del_minuto, resolvedor=resolvedor
                )
            )
            resolvedor.guardar()
            print(f"  {symbol}: listo ({len(resolvedor.velas_ambiguas_unicas())} velas ambiguas hasta ahora)", flush=True)
    resolvedor.guardar()

    if args.desde is not None:
        base = [o for o in base if o.dia >= args.desde]
        inversa = [o for o in inversa if o.dia >= args.desde]
        resuelta = [o for o in resuelta if o.dia >= args.desde]
        print(f"(Solo decisiones desde {args.desde}. La cobertura de arriba cuenta TODA la muestra.)")
    if not base:
        print("ERROR: no hay observaciones (¿falta el backfill de ~2 años en 1h y 1d?).", file=sys.stderr)
        return 1
    if args.solo_contar:
        imprimir_solo_contar(resolvedor, base)
        return 0
    imprimir(resolvedor, base, inversa, resuelta)
    return 2 if resolvedor.no_resueltas else 0


if __name__ == "__main__":
    raise SystemExit(main())
