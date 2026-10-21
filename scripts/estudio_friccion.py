"""Prueba de estrés de FRICCIÓN (slippage y huecos) del backtest walk-forward (2026-10-21).

Responde: ¿sobreviven el veredicto y la expectancy del score cuantitativo cuando la
ejecución es peor que la ideal? Repite el walk-forward con el MISMO score, ventanas
(12/3/3), bootstrap y semilla, cambiando solo la simulación de salida
(`app.data.exit_simulation.Friccion`). Los escenarios y la forma de leer los resultados
están PRE-REGISTRADOS en `claude/protocolo-friccion.md`: no se añaden ni se quitan
escenarios tras ver resultados.

Solo lectura: no escribe en la base de datos, no llama a la IA (costo cero), no cambia el
score ni ninguna regla viva. Es una prueba de SENSIBILIDAD con supuestos del usuario, no
una medición de la ejecución real.

Uso:
    python -m scripts.estudio_friccion                          # muestra completa
    python -m scripts.estudio_friccion --hasta-fecha 2025-03-19 # época no vista
    python -m scripts.estudio_friccion --escenarios E0,S25G     # subconjunto (solo para pruebas)

Control obligatorio: la línea E0 (sin fricción) debe coincidir con la PRUEBA AGREGADA de
`python -m scripts.backtest_walk_forward` (mismos argumentos) corrido el mismo día. Si no
coincide, el estudio no es válido.

Código de salida: 0 = informe completo; 1 = no se pudo correr (argumentos o datos).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from app.config import get_settings
from app.data.backtest_study import observaciones_de_simbolo
from app.data.candle_ingestion import velas_vencidas_que_invalidan_un_estudio
from app.data.exit_simulation import SIN_FRICCION, Friccion
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.domain.rules.walk_forward import InformeWalkForward, hasta_fecha, informe_walk_forward
from app.domain.symbols import ALLOWED_SYMBOLS
from app.logging_config import configure_logging
from app.repositories.candles import get_candles

_D = Decimal


@dataclass(frozen=True)
class Escenario:
    id: str
    friccion: Friccion
    descripcion: str


def _esc(id_: str, s: str, extra: str, huecos: bool, descripcion: str) -> Escenario:
    return Escenario(id_, Friccion(_D(s), _D(extra), huecos), descripcion)


ESCENARIOS: tuple[Escenario, ...] = (
    Escenario("E0", SIN_FRICCION, "control: sin fricción (resultados publicados)"),
    _esc("G", "0", "0", True, "solo huecos"),
    _esc("S10", "0.0010", "0", False, "slippage 0,10 % por lado"),
    _esc("S25", "0.0025", "0", False, "slippage 0,25 % por lado"),
    _esc("S50", "0.0050", "0", False, "slippage 0,50 % por lado"),
    _esc("S10G", "0.0010", "0", True, "slippage 0,10 % + huecos"),
    _esc("S25G", "0.0025", "0", True, "slippage 0,25 % + huecos"),
    _esc("S50G", "0.0050", "0", True, "slippage 0,50 % + huecos"),
    _esc("X", "0.0010", "0.0050", True, "stop castigado: 0,10 % + 0,50 pp extra en stop + huecos"),
)
"""Tabla FIJA del protocolo (`claude/protocolo-friccion.md`). Un test la compara con el protocolo."""

_SERIE_SIN_HUECOS = ("E0", "S10", "S25", "S50")
_SERIE_CON_HUECOS = ("G", "S10G", "S25G", "S50G")


def punto_de_equilibrio(puntos: list[tuple[Decimal, Decimal]]) -> tuple[str, Decimal | None]:
    """Slippage por lado donde la expectancy cruza 0, por INTERPOLACIÓN LINEAL entre
    puntos consecutivos `(s, expectancy)` ordenados por `s` creciente. APROXIMADO.

    Devuelve `("YA_NEGATIVA", None)` si ya es <= 0 en el primer punto, `("NO_CRUZA", None)` si
    sigue > 0 en el último, o `("CRUZA", s)` con el primer cruce."""
    if not puntos:
        raise ValueError("Hacen falta puntos para estimar el punto de equilibrio.")
    if puntos[0][1] <= 0:
        return "YA_NEGATIVA", None
    for (s0, e0), (s1, e1) in zip(puntos, puntos[1:]):
        if e1 <= 0:
            return "CRUZA", s0 + (s1 - s0) * e0 / (e0 - e1)
    return "NO_CRUZA", None


def _pct(valor: Decimal | None) -> str:
    return "   n/d" if valor is None else f"{valor * 100:+6.2f}%"


def fila(esc: Escenario, informe: InformeWalkForward) -> str:
    t = informe.prueba_agregada
    if t is None or t.discriminacion is None:
        return f"{esc.id:5s} sin datos suficientes para terciles ({esc.descripcion})"
    return (
        f"{esc.id:5s} n={t.n:5d} cens={t.censuradas:4d} alto={_pct(t.expectancy_alta)} bajo={_pct(t.expectancy_baja)} "
        f"dif={_pct(t.discriminacion)} IC95=[{_pct(t.ci_inferior).strip()}, {_pct(t.ci_superior).strip()}] "
        f"{informe.veredicto.value:14s} pruebas+={informe.pliegues_con_diferencia_positiva}/{len(informe.pliegues)}"
        f"  ({esc.descripcion})"
    )


def _linea_equilibrio(nombre: str, estado_valor: tuple[str, Decimal | None]) -> str:
    estado, valor = estado_valor
    if estado == "CRUZA":
        return f"  {nombre}: ~{valor * 100:.2f} % por lado (interpolación lineal; aproximado)"
    if estado == "YA_NEGATIVA":
        return f"  {nombre}: la expectancy del tercil alto ya es <= 0 sin slippage"
    return f"  {nombre}: no cruza 0 dentro de la grilla (hasta 0,50 % por lado)"


def resumen(resultados: dict[str, InformeWalkForward]) -> list[str]:
    """Veredictos que cambian respecto de E0 y puntos de equilibrio, solo con lo que hay."""
    lineas: list[str] = []
    base = resultados.get("E0")
    if base is not None:
        cambian = [i for i, inf in resultados.items() if i != "E0" and inf.veredicto != base.veredicto]
        lineas.append(
            "Veredicto vs. E0 (" + base.veredicto.value + "): "
            + ("cambia en " + ", ".join(cambian) if cambian else "no cambia en ningún escenario corrido")
        )
    for etiqueta, ids in (("sin huecos", _SERIE_SIN_HUECOS), ("con huecos", _SERIE_CON_HUECOS)):
        puntos = []
        for id_ in ids:
            inf = resultados.get(id_)
            if inf is None or inf.prueba_agregada is None or inf.prueba_agregada.expectancy_alta is None:
                puntos = []
                break
            esc = next(e for e in ESCENARIOS if e.id == id_)
            puntos.append((esc.friccion.slippage_por_lado, inf.prueba_agregada.expectancy_alta))
        if len(puntos) == len(ids):
            lineas.append(_linea_equilibrio(f"Equilibrio del tercil alto ({etiqueta})", punto_de_equilibrio(puntos)))
    return lineas


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", type=str, default=None, help="Subconjunto para pruebas (por defecto, los autorizados).")
    parser.add_argument(
        "--hasta-fecha", type=date.fromisoformat, default=None,
        help="Evaluar solo decisiones ANTERIORES a esta fecha (AAAA-MM-DD), p. ej. 2025-03-19 = época no vista.",
    )
    parser.add_argument("--escenarios", type=str, default=None, help="Ids separados por coma (por defecto, los 9 del protocolo).")
    args = parser.parse_args(argv)

    symbols = tuple(s.strip() for s in args.symbols.split(",")) if args.symbols else ALLOWED_SYMBOLS
    for s in symbols:
        if s not in ALLOWED_SYMBOLS:
            print(f"ERROR: {s} no está en la lista autorizada.", file=sys.stderr)
            return 1
    escenarios = ESCENARIOS
    if args.escenarios:
        pedidos = [e.strip() for e in args.escenarios.split(",")]
        desconocidos = [e for e in pedidos if e not in {x.id for x in ESCENARIOS}]
        if desconocidos:
            print(f"ERROR: escenario(s) desconocido(s): {', '.join(desconocidos)}.", file=sys.stderr)
            return 1
        escenarios = tuple(e for e in ESCENARIOS if e.id in pedidos)

    session_factory = get_sessionmaker(get_engine(get_settings()))
    series: dict[str, tuple[list, list]] = {}
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
            series[symbol] = (get_candles(session, symbol, "1d"), get_candles(session, symbol, "1h"))

    print("PRUEBA DE ESTRÉS DE FRICCIÓN del backtest walk-forward (protocolo: claude/protocolo-friccion.md)")
    print("Ventanas 12/3/3. Retornos netos por entrada; comisión 0,1 % por lado en todos los escenarios.")
    if args.hasta_fecha is not None:
        print(f"(Solo decisiones anteriores a {args.hasta_fecha}.)")
    print("Prueba agregada por escenario:\n")

    resultados: dict[str, InformeWalkForward] = {}
    for esc in escenarios:
        observaciones = []
        for symbol in symbols:
            diarias, horarias = series[symbol]
            observaciones.extend(observaciones_de_simbolo(symbol, diarias, horarias, friccion=esc.friccion))
        if args.hasta_fecha is not None:
            observaciones = hasta_fecha(observaciones, args.hasta_fecha)
        if not observaciones:
            print(f"ERROR: no hay observaciones para el escenario {esc.id}.", file=sys.stderr)
            return 1
        resultados[esc.id] = informe_walk_forward(observaciones)
        print(fila(esc, resultados[esc.id]), flush=True)

    print()
    for linea in resumen(resultados):
        print(linea)
    print(
        "\nCONTROL: compara la línea E0 con la PRUEBA AGREGADA de "
        "`python -m scripts.backtest_walk_forward" + (f" --hasta-fecha {args.hasta_fecha}" if args.hasta_fecha else "")
        + "` corrido hoy. Deben coincidir; si no, el estudio no es válido."
    )
    print(
        "Cautelas: supuestos del usuario, no mediciones; velas de 1 h (el hueco usa la apertura); s constante, "
        "sin impacto de mercado; IC optimistas (observaciones no independientes) y sin corrección por "
        "comparaciones múltiples; solo el score DETERMINISTA. No autoriza cambiar ninguna regla ni usar capital real."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
