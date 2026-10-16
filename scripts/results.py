"""Resultados de las operaciones paper (Paso 9): lista de operaciones cerradas,
métricas de rendimiento y validación de la capacidad predictiva del score.

Uso:
    python -m scripts.results trades [--serie todas]    # operaciones cerradas (estrategia y observación, etiquetadas)
    python -m scripts.results performance [--days 30] [--serie estrategia|observacion|todas]
    python -m scripts.results validate                  # ¿el tercio superior del score rinde mejor que el inferior?
                                                        # (estrategia; luego observación aparte; luego conjunto)

Series (decisión del 2026-10-12): las operaciones de OBSERVACIÓN (candidatos del
pre-filtro abiertos sin IA, 100 USD) se miden APARTE de las de la ESTRATEGIA. Su
puntaje es el del pre-filtro, otra escala: nunca se mezcla sin decirlo.
`performance` guarda en la base solo la serie de estrategia; las otras se
calculan y se muestran sin guardar.

Todo se calcula con funciones deterministas (`app/domain/rules/results.py`);
este script solo imprime. `performance` guarda las métricas en
`performance_metrics` (tabla derivada y reconstruible: recalcular el mismo
periodo reemplaza las filas). `validate` no guarda nada.

Sin `--days`, `performance` cubre desde la primera operación hasta ahora.
Cada comando es una sola transacción.
"""

from __future__ import annotations

import argparse
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.enums import SerieOperacion
from app.domain.rules.results import (
    EstadoValidacion,
    PerformanceMetrics,
    StratifiedValidationReport,
    ValidationReport,
)
from app.logging_config import configure_logging
from app.repositories.trade_results import (
    get_trade_result_by_position,
    list_trade_results,
    list_versions_for_position,
)
from app.results.performance_service import compute_performance, history_start, recalculate_performance
from app.results.trade_result_service import EstadoCorreccion, corregir_trade_result
from app.results.validation_service import validate_score, validate_score_stratified


_SERIES: dict[str, SerieOperacion | None] = {
    "estrategia": SerieOperacion.ESTRATEGIA,
    "observacion": SerieOperacion.OBSERVACION,
    "todas": None,
}
_ETIQUETA_SERIE = {SerieOperacion.ESTRATEGIA: "estrategia", SerieOperacion.OBSERVACION: "observación"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="comando", required=True)
    trades = sub.add_parser("trades", help="Lista las operaciones cerradas.")
    trades.add_argument("--serie", choices=list(_SERIES), default="todas", help="Qué serie listar (por defecto, ambas).")
    perf = sub.add_parser("performance", help="Recalcula y muestra las métricas de rendimiento.")
    perf.add_argument("--days", type=int, default=None, help="Solo los últimos N días (por defecto, todo).")
    perf.add_argument(
        "--serie", choices=list(_SERIES), default="estrategia",
        help="Serie a medir (por defecto, la estrategia; solo esa se guarda en la base).",
    )
    sub.add_parser("validate", help="Validación de la capacidad predictiva del score.")

    corr = sub.add_parser(
        "corregir",
        help="Corrección auditable de una operación: recalcula y, si difiere, registra una versión nueva.",
    )
    corr.add_argument("--position", required=True, help="UUID de la posición a corregir.")
    corr.add_argument("--motivo", required=True, help="Motivo de la corrección (obligatorio; queda en el registro).")
    corr.add_argument("--autor", default=None, help="Quién ordena la corrección (opcional).")
    corr.add_argument(
        "--confirmar", action="store_true",
        help="Aplica la corrección. Sin esta bandera es una simulación: muestra qué cambiaría sin escribir nada.",
    )

    ver = sub.add_parser("versiones", help="Historial de versiones de una operación (auditoría).")
    ver.add_argument("--position", required=True, help="UUID de la posición.")
    return parser.parse_args(argv)


def _fmt(valor, sufijo: str = "", decimales: int | None = 2) -> str:
    """Solo presentación: nunca notación científica (`0E-8`) y, salvo que se pida
    `decimales=None` (precios, que pueden necesitar muchos), 2 decimales. No cambia
    ningún valor guardado ni calculado."""
    if valor is None:
        return "n/d"
    if isinstance(valor, Decimal):
        if decimales is None:
            texto = format(valor.normalize(), "f")
        else:
            texto = format(valor.quantize(Decimal(1).scaleb(-decimales)), "f")
        return f"{texto}{sufijo}"
    return f"{valor}{sufijo}"


def format_metrics(m: PerformanceMetrics) -> list[str]:
    alcance = m.scope_type if m.scope_key is None else f"{m.scope_type} {m.scope_key}"
    return [
        f"[{alcance}] operaciones={m.total_trades} (ganadoras {m.winning_trades}, perdedoras {m.losing_trades}, "
        f"equilibrio {m.breakeven_trades})  win rate={_fmt(m.win_rate_pct, '%')}",
        f"    P&L neto={_fmt(m.net_pnl)} USD (bruto {_fmt(m.gross_pnl)}, comisiones {_fmt(m.total_fees)})  "
        f"expectancy={_fmt(m.expectancy_per_trade, ' USD')}  profit factor={_fmt(m.profit_factor)}  "
        f"R medio={_fmt(m.average_r_multiple)}",
        f"    drawdown máximo={_fmt(m.max_drawdown_pct, '%')}  benchmark BTC: retorno={_fmt(m.benchmark_return_pct, '%')} "
        f"drawdown={_fmt(m.benchmark_max_drawdown_pct, '%')}",
        f"    score alto vs bajo: expectancy {_fmt(m.high_score_expectancy)} vs {_fmt(m.low_score_expectancy)} USD  "
        f"discriminación={_fmt(m.score_discrimination)}",
    ]


def format_validation(r: ValidationReport) -> list[str]:
    lineas = [f"Estado: {r.status.value}  operaciones={r.n_trades} (con score: {r.n_trades_con_score})"]
    if r.status == EstadoValidacion.MUESTRA_INSUFICIENTE:
        lineas.append("No se puede formar el tercio superior e inferior todavía.")
    else:
        lineas += [
            f"Tercil de {r.tercil_size} operaciones: expectancy alta {_fmt(r.high_score_expectancy)} USD, "
            f"baja {_fmt(r.low_score_expectancy)} USD  ->  discriminación {_fmt(r.score_discrimination)} USD",
            f"IC {r.confidence_level_pct}% por bootstrap ({r.iterations} iteraciones, semilla {r.seed}): "
            f"[{_fmt(r.ci_lower)}, {_fmt(r.ci_upper)}]",
            "Discrimina (límite inferior > 0): " + ("SÍ" if r.discriminates else "no")
            + ("  [orientativo: muestra preliminar]" if r.status == EstadoValidacion.PRELIMINAR else ""),
        ]
    lineas += [f"Nota: {n}" for n in r.notas]
    return lineas


def format_stratified(r: StratifiedValidationReport) -> list[str]:
    i = r.informe
    lineas = [
        "--- Medición adicional estratificada por mes (enmienda E4) ---",
        f"Estado: {i.status.value}  meses usados={r.estratos_usados}, excluidos={r.estratos_excluidos} "
        f"({r.operaciones_excluidas} operación(es))  operaciones usadas={i.n_trades_con_score}",
    ]
    if i.tercil_size is not None:
        lineas += [
            f"Desviación respecto del mes: alta {_fmt(i.high_score_expectancy)} USD, baja {_fmt(i.low_score_expectancy)} USD "
            f"->  discriminación {_fmt(i.score_discrimination)} USD",
            f"IC {i.confidence_level_pct}% por bootstrap: [{_fmt(i.ci_lower)}, {_fmt(i.ci_upper)}]  "
            "Discrimina (límite inferior > 0): " + ("SÍ" if i.discriminates else "no"),
        ]
    if r.alerta_de_regimen:
        lineas.append("AVISO: la medición principal discrimina y la estratificada no: posible efecto de régimen de mercado.")
    lineas += [f"Nota: {n}" for n in i.notas]
    return lineas


def run_command(session: Session, args: argparse.Namespace, *, now: datetime, initial_capital: Decimal) -> list[str]:
    """Ejecuta el comando y devuelve las líneas a imprimir (separado de `main`
    para probarlo sin sesión propia)."""
    if args.comando == "trades":
        serie = _SERIES[args.serie]
        series = [serie] if serie is not None else [SerieOperacion.ESTRATEGIA, SerieOperacion.OBSERVACION]
        filas = sorted(
            ((s, r) for s in series for r in list_trade_results(session, serie=s)),
            key=lambda par: (par[1].exit_time, par[1].id),
        )
        if not filas:
            return ["Todavía no hay operaciones cerradas."]
        return [
            f"{r.exit_time:%Y-%m-%d %H:%M} [{_ETIQUETA_SERIE[s]}] {r.symbol:<9} score={_fmt(r.initial_opportunity_score)} "
            f"entrada={_fmt(r.entry_price, decimales=None)} salida={_fmt(r.exit_price, decimales=None)} "
            f"P&L neto={_fmt(r.net_pnl)} USD ({_fmt(r.net_return_pct)}%) "
            f"R={_fmt(r.realized_r_multiple)} MAE={_fmt(r.mae_pct, '%')} MFE={_fmt(r.mfe_pct, '%')} "
            f"{r.thesis_status.value} {r.exit_reason.value}"
            for s, r in filas
        ]

    if args.comando == "performance":
        serie = _SERIES[args.serie]
        if args.days is not None:
            inicio = now - timedelta(days=args.days)
        else:
            inicio = history_start(session, now=now, serie=serie)
        if serie == SerieOperacion.ESTRATEGIA:
            metricas = recalculate_performance(
                session, period_start=inicio, period_end=now, initial_capital=initial_capital, now=now
            )
            nota = "Serie: estrategia (guardada en performance_metrics)."
        else:
            metricas = compute_performance(
                session, period_start=inicio, period_end=now, initial_capital=initial_capital, serie=serie
            )
            nombre = "observación" if serie == SerieOperacion.OBSERVACION else "conjunta (estrategia + observación)"
            nota = f"Serie: {nombre} — solo cálculo, NO se guarda; sin curva de equity propia, el drawdown máximo es n/d."
        lineas: list[str] = []
        for m in metricas:
            lineas += format_metrics(m)
        return [f"Periodo: {inicio:%Y-%m-%d %H:%M} a {now:%Y-%m-%d %H:%M} UTC. {nota}", *lineas]

    if args.comando == "validate":
        lineas = []
        for titulo, serie in (
            ("ESTRATEGIA (la que mide el plan)", SerieOperacion.ESTRATEGIA),
            ("OBSERVACIÓN (puntaje del pre-filtro; serie aparte)", SerieOperacion.OBSERVACION),
            ("CONJUNTA (solo comparación: mezcla dos escalas de puntaje)", None),
        ):
            if lineas:
                lineas.append("")
            lineas.append(f"===== {titulo} =====")
            lineas += format_validation(validate_score(session, serie=serie))
            lineas.append("")
            lineas += format_stratified(validate_score_stratified(session, serie=serie))
        return lineas

    if args.comando == "corregir":
        return _correr_correccion(session, args, initial_capital=initial_capital)

    if args.comando == "versiones":
        return _correr_versiones(session, args)

    raise ValueError(f"Comando desconocido: {args.comando!r}")  # pragma: no cover - argparse lo impide


def _correr_correccion(session: Session, args: argparse.Namespace, *, initial_capital: Decimal) -> list[str]:
    try:
        position_id = uuid.UUID(args.position)
    except ValueError:
        return [f"UUID de posición inválido: {args.position!r}"]
    r = corregir_trade_result(
        session,
        position_id,
        motivo=args.motivo,
        autor=args.autor,
        initial_capital=initial_capital,
        dry_run=not args.confirmar,
    )
    if r.estado == EstadoCorreccion.SIN_RESULTADO:
        return [f"La posición {position_id} no tiene un resultado que corregir (genera uno primero)."]
    if r.estado == EstadoCorreccion.SIN_CAMBIOS:
        return ["El recálculo coincide con la versión vigente: no hay nada que corregir."]
    cabecera = "SIMULACIÓN (nada se escribió; usa --confirmar para aplicar)" if not args.confirmar else "Corrección aplicada"
    lineas = [cabecera, f"Campos que cambian ({len(r.campos_cambiados)}): {', '.join(r.campos_cambiados)}"]
    if args.confirmar:
        lineas.append(f"Versión anterior {r.version_anterior_id} -> versión nueva {r.version_nueva_id} (motivo: {args.motivo})")
    else:
        lineas.append(f"Versión vigente actual: {r.version_anterior_id}")
    return lineas


def _correr_versiones(session: Session, args: argparse.Namespace) -> list[str]:
    try:
        position_id = uuid.UUID(args.position)
    except ValueError:
        return [f"UUID de posición inválido: {args.position!r}"]
    versiones = list_versions_for_position(session, position_id)
    if not versiones:
        return [f"La posición {position_id} no tiene resultados."]
    vigente = get_trade_result_by_position(session, position_id)
    lineas = [f"Historial de la posición {position_id} ({len(versiones)} versión/es):"]
    for i, v in enumerate(versiones):
        marca = "  <- VIGENTE" if vigente is not None and v.id == vigente.id else ""
        tipo = "original" if v.supersedes_id is None else f"corrige {v.supersedes_id}"
        lineas.append(
            f"{i + 1}. {v.id} [{tipo}] creada {v.created_at:%Y-%m-%d %H:%M} UTC  "
            f"P&L neto={_fmt(v.net_pnl)} USD{marca}"
        )
        if v.correction_reason:
            autor = f" por {v.corrected_by}" if v.corrected_by else ""
            lineas.append(f"     motivo{autor}: {v.correction_reason}")
    return lineas


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = parse_args(argv)
    settings = get_settings()
    session_factory = get_sessionmaker(get_engine(settings))
    now = datetime.now(timezone.utc)
    with session_scope(session_factory) as session:
        for linea in run_command(session, args, now=now, initial_capital=settings.paper_capital_usd):
            print(linea)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
