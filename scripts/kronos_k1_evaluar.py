"""K1 — evalúa los pronósticos generados con las reglas del protocolo (enmienda E6).

    docker compose --profile kronos run --rm kronos python -m scripts.kronos_k1_evaluar

Solo LEE (transacción READ ONLY): pronósticos de `kronos_forecasts` y velas. No usa
el modelo. Imprime tres bloques con la MISMA maquinaria:

1. PRIMARIO (fechas >= 2024-07-01): trae el veredicto del protocolo.
2. SENSIBILIDAD (fechas >= 2025-03-19): informativo.
3. DIAGNÓSTICO PRE-CORTE (fechas < 2024-07-01): informativo; mide posible memoria.

Si la generación está incompleta, el resultado es PARCIAL y no es el veredicto del
protocolo. Guarda `data/kronos/k1-evaluacion-<fecha>.json`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from app.domain.rules.kronos_k1 import K1_SENSIBILIDAD_DESDE, InformeK1, evaluar_k1
from app.experiments.kronos import k1
from app.experiments.kronos.k1 import PronosticoGuardado, construir_observaciones_k1
from app.experiments.kronos.k1_informe import formatear_informe_k1, informe_a_json
from app.experiments.kronos.pesos import RUTA_LOCK_POR_DEFECTO, PesosNoFijados, leer_lock


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m scripts.kronos_k1_evaluar", description=__doc__.split("\n\n")[0])
    p.add_argument("--lock", type=Path, default=RUTA_LOCK_POR_DEFECTO)
    p.add_argument("--salida", type=Path, default=None)
    return p.parse_args(argv)


def evaluar(pronosticos, diarias, velas_1h, *, fechas_fijadas: dict | None = None) -> tuple[list[str], dict]:
    """Núcleo testeable: sin base ni modelo. Devuelve (líneas de texto, datos JSON)."""
    corte = k1.PRIMERA_FECHA
    # Los descartes se cuentan POR CONJUNTO (primario / pre-corte): antes se sumaban todos y se
    # mostraban bajo el bloque primario, atribuyéndole descartes del diagnóstico.
    def _es_primario(p) -> bool:
        return p.as_of.astimezone(timezone.utc).date() >= corte

    obs_prim, descartes_prim = construir_observaciones_k1([p for p in pronosticos if _es_primario(p)], diarias, velas_1h)
    obs_pre, descartes_pre = construir_observaciones_k1([p for p in pronosticos if not _es_primario(p)], diarias, velas_1h)
    obs = [*obs_prim, *obs_pre]
    grupos = {
        "PRIMARIO — protocolo K1 v1 (fechas >= 2024-07-01)": ([o for o in obs if o.dia >= corte], True),
        f"SENSIBILIDAD (fechas >= {K1_SENSIBILIDAD_DESDE.isoformat()})": ([o for o in obs if o.dia >= K1_SENSIBILIDAD_DESDE], False),
        "DIAGNÓSTICO PRE-CORTE (fechas < 2024-07-01; posible memoria del preentrenamiento)": ([o for o in obs if o.dia < corte], False),
    }
    lineas: list[str] = []
    datos: dict = {"descartes": {"primario": descartes_prim, "pre_corte": descartes_pre}, "informes": {}}
    if fechas_fijadas is not None:
        con_pronostico = {p.as_of.astimezone(timezone.utc).date() for p in pronosticos}
        faltan = [d for d in fechas_fijadas["primarias"] if d not in con_pronostico]
        datos["fechas_primarias_sin_pronostico"] = len(faltan)
        if faltan:
            lineas.append(
                f"AVISO: GENERACIÓN INCOMPLETA — {len(faltan)} de {len(fechas_fijadas['primarias'])} fechas primarias "
                "aún no tienen pronósticos. Lo que sigue es PARCIAL y NO es el veredicto del protocolo."
            )
    for titulo, (subconjunto, decisivo) in grupos.items():
        informe: InformeK1 = evaluar_k1(subconjunto)
        if decisivo:
            descartes_del_bloque = descartes_prim
        elif titulo.startswith("DIAGNÓSTICO"):
            descartes_del_bloque = descartes_pre
        else:
            descartes_del_bloque = {}  # la sensibilidad es un subconjunto del primario: mismos descartes
        lineas += formatear_informe_k1(titulo, informe, descartes_del_bloque, decisivo=decisivo)
        lineas.append("")
        datos["informes"][titulo] = informe_a_json(informe)
    return lineas, datos


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - requiere base
    args = parse_args(argv)
    try:
        lock = leer_lock(args.lock)
    except PesosNoFijados as e:
        print(str(e), file=sys.stderr)
        return 2

    from app.config import get_settings
    from app.db.session import get_engine, get_sessionmaker
    from app.domain.symbols import ALLOWED_SYMBOLS
    from app.repositories.candles import get_candles
    from app.repositories.health import set_transaction_read_only
    from app.repositories.kronos_forecasts import listar_forecasts

    params = k1.parametros_k1(lock)
    session = get_sessionmaker(get_engine(get_settings()))()
    try:
        set_transaction_read_only(session)
        filas = listar_forecasts(
            session, weights_revision=params.weights_revision, lookback=params.lookback, horizon=params.horizon,
            n_trajectories=params.n_trajectories, seed=params.seed,
        )
        if not filas:
            print("No hay pronósticos guardados. Primero: python -m scripts.kronos_k1_generar", file=sys.stderr)
            return 1
        simbolos = sorted({f.symbol for f in filas} & set(ALLOWED_SYMBOLS))
        velas_1h = {s: get_candles(session, s, "1h") for s in simbolos}
        diarias = {s: get_candles(session, s, "1d") for s in simbolos}
    finally:
        session.rollback()
        session.close()

    pronosticos = [PronosticoGuardado(f.symbol, f.as_of, f.last_close, f.expected_return) for f in filas]
    fijadas = None
    if k1.RUTA_FECHAS.exists():
        crudo = json.loads(k1.RUTA_FECHAS.read_text(encoding="utf-8"))
        fijadas = {k: [date.fromisoformat(x) for x in crudo[k]] for k in ("primarias", "diagnostico")}
    lineas, datos = evaluar(pronosticos, diarias, velas_1h, fechas_fijadas=fijadas)
    print(f"Pronósticos leídos: {len(filas)} · huella del protocolo en las filas: {filas[0].protocol_sha256[:12]}…")
    for l in lineas:
        print(l)
    ahora = datetime.now(timezone.utc)
    salida = args.salida or Path(f"data/kronos/k1-evaluacion-{ahora:%Y%m%d-%H%M}.json")
    salida.parent.mkdir(parents=True, exist_ok=True)
    datos["generado_el"] = ahora.isoformat()
    salida.write_text(json.dumps(datos, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Informe completo: {salida}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
