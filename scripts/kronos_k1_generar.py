"""K1 — genera los pronósticos del backtest pre-registrado (enmienda E6).

    docker compose --profile kronos run --rm kronos python -m scripts.kronos_k1_generar --max-minutos 480

Es REANUDABLE: cada pronóstico se guarda en `kronos_forecasts` apenas se calcula y
lo ya hecho se salta. Se puede detener con Ctrl+C o con `--max-minutos` y volver a
lanzar el mismo comando. El orden es determinista (fecha, símbolo).

Los parámetros (modelo, 8 trayectorias, T, top_p, semilla, lookback, horizonte,
fechas cada 3 días desde 2024-07-01) están CONGELADOS por `docs/protocolo-k1-kronos.md`
y no son argumentos. La lista de fechas se fija en `data/kronos/k1_fechas.json` la
primera vez (con la huella SHA-256 del protocolo) y no cambia. Se refusa continuar si
el protocolo se modificó.

Solo escribe en `kronos_forecasts`. No toca ninguna tabla operativa.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.data.exchange_client import Candle
from app.domain.symbols import ALLOWED_SYMBOLS
from app.experiments.kronos import k1
from app.experiments.kronos.contexto import ContextoKronos
from app.experiments.kronos.pesos import RUTA_LOCK_POR_DEFECTO, LockPesos, PesosNoFijados, leer_lock
from app.experiments.kronos.predictor import PredictorDeTrayectorias
from app.domain.rules.kronos_signal import ParametrosKronos, ResumenKronos
from app.repositories.kronos_forecasts import ForecastRow, append_forecast, claves_existentes


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m scripts.kronos_k1_generar", description=__doc__.split("\n\n")[0])
    p.add_argument("--conjunto", choices=("primaria", "diagnostico", "ambos"), default="ambos",
                   help="Qué fechas generar (primero la primaria, luego el diagnóstico pre-corte).")
    p.add_argument("--max-minutos", type=float, default=None, help="Se detiene limpiamente tras N minutos (reanudable).")
    p.add_argument("--carpeta-pesos", type=Path, default=Path("data/kronos/pesos"))
    p.add_argument("--lock", type=Path, default=RUTA_LOCK_POR_DEFECTO)
    p.add_argument("--dispositivo", default="cpu")
    return p.parse_args(argv)


def fecha_a_generar(fechas: dict, conjunto: str) -> list:
    if conjunto == "primaria":
        return list(fechas["primarias"])
    if conjunto == "diagnostico":
        return list(fechas["diagnostico"])
    return [*fechas["primarias"], *fechas["diagnostico"]]


def fila_para_guardar(
    ctx: ContextoKronos, resumen: ResumenKronos, cierres, *, params: ParametrosKronos, dispositivo: str,
    entorno: str, huella: str,
) -> ForecastRow:
    return ForecastRow(
        symbol=ctx.symbol, as_of=ctx.as_of, model_id=params.model_id, weights_revision=params.weights_revision,
        tokenizer_id=params.tokenizer_id, tokenizer_revision=params.tokenizer_revision, code_commit=params.code_commit,
        lookback=params.lookback, horizon=params.horizon, n_trajectories=params.n_trajectories,
        temperature=params.temperature, top_p=params.top_p, seed=params.seed, uses_volume=params.usa_volumen,
        device=dispositivo, environment=entorno, protocol_sha256=huella, last_close=ctx.ultimo_cierre,
        final_closes=tuple(cierres), expected_return=resumen.expected_return, prob_up=resumen.prob_up,
        dispersion=resumen.dispersion,
    )


def ejecutar(
    args: argparse.Namespace,
    *,
    predictor: PredictorDeTrayectorias,
    velas_1h: dict[str, list[Candle]],
    lock: LockPesos,
    session,
    ahora: datetime,
    entorno: str,
    ruta_fechas: Path = k1.RUTA_FECHAS,
    ruta_protocolo: Path = k1.RUTA_PROTOCOLO,
    progreso=None,
) -> k1.ResultadoGeneracion:
    """Núcleo testeable: predictor y sesión inyectados (sin torch ni red)."""
    huella = k1.huella_protocolo(ruta_protocolo)
    params = k1.parametros_k1(lock)
    fechas = k1.cargar_o_fijar_fechas(
        ruta_fechas, huella,
        lambda: {"primarias": k1.fechas_primarias(k1.ultima_fecha_posible(velas_1h)), "diagnostico": k1.fechas_diagnostico()},
        ahora=ahora,
    )
    ya = claves_existentes(
        session, weights_revision=params.weights_revision, lookback=params.lookback, horizon=params.horizon,
        n_trajectories=params.n_trajectories, seed=params.seed,
    )

    def guardar(ctx, resumen, cierres):
        append_forecast(session, fila_para_guardar(ctx, resumen, cierres, params=params, dispositivo=args.dispositivo,
                                                   entorno=entorno, huella=huella))
        session.commit()  # un pronóstico por transacción: si el proceso muere, no se pierde lo hecho

    return k1.generar_pronosticos(
        velas_1h, fechas=fecha_a_generar(fechas, args.conjunto), simbolos=list(ALLOWED_SYMBOLS), predictor=predictor,
        params=params, ya_hechos=ya, guardar=guardar,
        max_segundos=None if args.max_minutos is None else args.max_minutos * 60, progreso=progreso,
    )


def _mostrar_progreso(hechos: int, total: int, segundos: float) -> None:  # pragma: no cover - consola
    if hechos == 1 or hechos % 10 == 0 or hechos == total:
        restante = segundos / hechos * (total - hechos)
        print(f"  {hechos}/{total} · {segundos / 60:.1f} min · ~{restante / 3600:.1f} h restantes", flush=True)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - requiere pesos reales y base
    args = parse_args(argv)
    os.environ["HF_HUB_OFFLINE"] = "1"
    from scripts.kronos_k0 import commit_del_codigo, versiones_del_entorno

    try:
        lock = leer_lock(args.lock)
    except PesosNoFijados as e:
        print(str(e), file=sys.stderr)
        return 2
    if lock.code_commit != commit_del_codigo():
        print("El commit de Kronos de la imagen no coincide con el del bloqueo; reconstruye la imagen.", file=sys.stderr)
        return 2

    from app.config import get_settings
    from app.db.session import get_engine, get_sessionmaker
    from app.experiments.kronos.predictor import KronosTorchPredictor
    from app.repositories.candles import get_candles

    session = get_sessionmaker(get_engine(get_settings()))()
    try:
        print("Leyendo velas de 1 h…", flush=True)
        velas = {s: get_candles(session, s, "1h") for s in ALLOWED_SYMBOLS}
        session.rollback()
        predictor = KronosTorchPredictor(
            lock, carpeta_modelo=args.carpeta_pesos / "modelo", carpeta_tokenizador=args.carpeta_pesos / "tokenizador",
            device=args.dispositivo,
        )
        r = ejecutar(
            args, predictor=predictor, velas_1h=velas, lock=lock, session=session, ahora=datetime.now(timezone.utc),
            entorno=json.dumps(versiones_del_entorno(), sort_keys=True), progreso=_mostrar_progreso,
        )
    finally:
        session.close()
    print(
        f"Generados: {r.generados} · ya existían: {r.ya_existian} · pendientes: {r.pendientes} · "
        f"sin contexto: {r.sin_contexto or 0} · {r.segundos / 60:.1f} min"
    )
    if r.detenido_por_tiempo or r.pendientes:
        print("Quedan pronósticos pendientes: vuelve a lanzar el MISMO comando para continuar.")
    else:
        print("Generación completa para este conjunto. Siguiente: python -m scripts.kronos_k1_evaluar")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
