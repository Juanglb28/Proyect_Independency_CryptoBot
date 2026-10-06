"""Resultados de las operaciones paper (Paso 9): lista de operaciones cerradas,
métricas de rendimiento y validación de la capacidad predictiva del score.

Uso:
    python -m scripts.results trades                    # operaciones cerradas
    python -m scripts.results performance [--days 30]   # recalcula y muestra las métricas
    python -m scripts.results validate                  # ¿el tercio superior del score rinde mejor que el inferior?

Todo se calcula con funciones deterministas (`app/domain/rules/results.py`);
este script solo imprime. `performance` guarda las métricas en
`performance_metrics` (tabla derivada y reconstruible: recalcular el mismo
periodo reemplaza las filas). `validate` no guarda nada.

Sin `--days`, `performance` cubre desde la primera operación hasta ahora.
Cada comando es una sola transacción.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.rules.results import EstadoValidacion, PerformanceMetrics, ValidationReport
from app.logging_config import configure_logging
from app.repositories.trade_results import list_trade_results
from app.results.performance_service import recalculate_performance
from app.results.validation_service import validate_score


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("trades", help="Lista las operaciones cerradas.")
    perf = sub.add_parser("performance", help="Recalcula y muestra las métricas de rendimiento.")
    perf.add_argument("--days", type=int, default=None, help="Solo los últimos N días (por defecto, todo).")
    sub.add_parser("validate", help="Validación de la capacidad predictiva del score.")
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


def run_command(session: Session, args: argparse.Namespace, *, now: datetime, initial_capital: Decimal) -> list[str]:
    """Ejecuta el comando y devuelve las líneas a imprimir (separado de `main`
    para probarlo sin sesión propia)."""
    if args.comando == "trades":
        filas = list_trade_results(session)
        if not filas:
            return ["Todavía no hay operaciones cerradas."]
        return [
            f"{r.exit_time:%Y-%m-%d %H:%M} {r.symbol:<9} score={_fmt(r.initial_opportunity_score)} "
            f"entrada={_fmt(r.entry_price, decimales=None)} salida={_fmt(r.exit_price, decimales=None)} "
            f"P&L neto={_fmt(r.net_pnl)} USD ({_fmt(r.net_return_pct)}%) "
            f"R={_fmt(r.realized_r_multiple)} MAE={_fmt(r.mae_pct, '%')} MFE={_fmt(r.mfe_pct, '%')} "
            f"{r.thesis_status.value} {r.exit_reason.value}"
            for r in filas
        ]

    if args.comando == "performance":
        if args.days is not None:
            inicio = now - timedelta(days=args.days)
        else:
            filas = list_trade_results(session)
            inicio = min((r.entry_time for r in filas), default=now)
        lineas: list[str] = []
        for m in recalculate_performance(
            session, period_start=inicio, period_end=now, initial_capital=initial_capital, now=now
        ):
            lineas += format_metrics(m)
        return [f"Periodo: {inicio:%Y-%m-%d %H:%M} a {now:%Y-%m-%d %H:%M} UTC", *lineas]

    if args.comando == "validate":
        return format_validation(validate_score(session))

    raise ValueError(f"Comando desconocido: {args.comando!r}")  # pragma: no cover - argparse lo impide


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
