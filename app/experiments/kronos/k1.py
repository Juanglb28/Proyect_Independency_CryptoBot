"""K1 — backtest histórico pre-registrado de Kronos (enmienda E6; `docs/protocolo-k1-kronos.md` v1).

Dos mitades, ambas SIN red y sin tocar ejecución:

1. GENERAR pronósticos (`generar_pronosticos`): reanudable, uno por llamada al
   predictor, con los parámetros congelados del protocolo. El guardado es una
   función inyectada (el script la conecta a `kronos_forecasts`; los tests, a una
   lista), así este módulo no importa base de datos ni repositorios.
2. CONSTRUIR observaciones (`construir_observaciones_k1`): une cada pronóstico
   guardado con el desenlace simulado del arnés del backtest del score
   (`observaciones_de_simbolo`: entrada al cierre de las 23:00 del día anterior,
   `simular_salida`) y con las líneas base (score v1, momentum 24 h y 7 d). Solo
   lee velas; el desenlace se mira DESPUÉS y nunca entra al pronóstico.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.data.backtest_study import observaciones_de_simbolo
from app.data.exchange_client import Candle
from app.domain.rules.kronos_k1 import ObservacionK1
from app.domain.rules.kronos_signal import ParametrosKronos, ResumenKronos, resumir_trayectorias, validar_parametros
from app.experiments.kronos.contexto import ContextoKronos, MotivoSinContexto, construir_contexto
from app.experiments.kronos.pesos import LockPesos
from app.experiments.kronos.predictor import PredictorDeTrayectorias

UNA_HORA = timedelta(hours=1)

# --- Parámetros CONGELADOS por el protocolo (no son argumentos de línea de comandos) ---
LOOKBACK = 400
HORIZONTE = 24
TRAYECTORIAS = 8
TEMPERATURA = Decimal("1.0")
TOP_P = Decimal("0.9")
SEMILLA_BASE = 20261015
PRIMERA_FECHA = date(2024, 7, 1)
PASO_DIAS = 3
DIAGNOSTICO_DESDE = date(2023, 7, 1)
DIAGNOSTICO_HASTA = date(2024, 6, 30)
DIAGNOSTICO_PASO_DIAS = 15

RUTA_PROTOCOLO = Path("docs/protocolo-k1-kronos.md")
RUTA_FECHAS = Path("data/kronos/k1_fechas.json")


def parametros_k1(lock: LockPesos) -> ParametrosKronos:
    """Parámetros del protocolo + las versiones EXACTAS del bloqueo de pesos."""
    p = ParametrosKronos(
        model_id=lock.modelo.repo_id,
        tokenizer_id=lock.tokenizador.repo_id,
        weights_revision=lock.modelo.revision,
        tokenizer_revision=lock.tokenizador.revision,
        code_commit=lock.code_commit,
        lookback=LOOKBACK,
        horizon=HORIZONTE,
        n_trajectories=TRAYECTORIAS,
        temperature=TEMPERATURA,
        top_p=TOP_P,
        seed=SEMILLA_BASE,
    )
    validar_parametros(p)
    return p


def huella_protocolo(ruta: Path = RUTA_PROTOCOLO) -> str:
    """SHA-256 del protocolo vigente: queda en cada pronóstico y en la lista de fechas.
    Los saltos de línea se normalizan (CRLF -> LF) para que Git o Windows no cambien la
    huella de un texto idéntico."""
    return hashlib.sha256(ruta.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


# --- Fechas de decisión ------------------------------------------------------------

def _medianoche(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def fechas_primarias(ultima: date) -> list[date]:
    """2024-07-01 y cada 3 días, hasta `ultima` (incluida si cae en la rejilla)."""
    salida, d = [], PRIMERA_FECHA
    while d <= ultima:
        salida.append(d)
        d += timedelta(days=PASO_DIAS)
    return salida


def fechas_diagnostico() -> list[date]:
    """Diagnóstico de contaminación: cada 15 días entre 2023-07-01 y 2024-06-30."""
    salida, d = [], DIAGNOSTICO_DESDE
    while d <= DIAGNOSTICO_HASTA:
        salida.append(d)
        d += timedelta(days=DIAGNOSTICO_PASO_DIAS)
    return salida


def ultima_fecha_posible(velas_1h: Mapping[str, Sequence[Candle]]) -> date:
    """Última fecha de decisión cuyo contexto puede existir: la medianoche más reciente
    que no supera el final de la última vela CERRADA común a todas las monedas."""
    finales = [
        max(v["open_time"] for v in velas if v.get("cerrada", True)) + UNA_HORA
        for velas in velas_1h.values()
        if velas
    ]
    if not finales:
        raise ValueError("No hay velas de 1 h.")
    return min(finales).astimezone(timezone.utc).date()


def cargar_o_fijar_fechas(
    ruta: Path, huella: str, calcular: Callable[[], dict[str, list[date]]], *, ahora: datetime
) -> dict[str, list[date]]:
    """Las fechas se calculan UNA vez y se guardan: la muestra no cambia aunque lleguen
    velas nuevas. Si el protocolo cambió desde que se fijaron, se rechaza."""
    if ruta.exists():
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        if datos["protocolo_sha256"] != huella:
            raise ValueError(
                "El protocolo K1 cambió desde que se fijaron las fechas: es un experimento nuevo, no una "
                "continuación. No se reanuda."
            )
        return {k: [date.fromisoformat(x) for x in datos[k]] for k in ("primarias", "diagnostico")}
    fechas = calcular()
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(
        json.dumps(
            {
                "protocolo_sha256": huella,
                "fijado_el": ahora.isoformat(),
                **{k: [d.isoformat() for d in v] for k, v in fechas.items()},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return fechas


# --- Generación reanudable -----------------------------------------------------------

@dataclass(frozen=True)
class ResultadoGeneracion:
    generados: int
    ya_existian: int
    pendientes: int  # contextos utilizables aún sin pronóstico (si se detuvo por tiempo)
    sin_contexto: dict[str, int]
    detenido_por_tiempo: bool
    segundos: float


Guardar = Callable[[ContextoKronos, ResumenKronos, Sequence[Decimal]], None]


def generar_pronosticos(
    velas_1h: Mapping[str, Sequence[Candle]],
    *,
    fechas: Sequence[date],
    simbolos: Sequence[str],
    predictor: PredictorDeTrayectorias,
    params: ParametrosKronos,
    ya_hechos: set[tuple[str, datetime]],
    guardar: Guardar,
    max_segundos: float | None = None,
    reloj: Callable[[], float] = time.perf_counter,
    progreso: Callable[[int, int, float], None] | None = None,
) -> ResultadoGeneracion:
    """Pronostica, en orden (fecha, símbolo), lo que aún no existe. Cada pronóstico se
    entrega a `guardar` apenas se calcula (si el proceso muere, no se pierde lo hecho).
    Un contexto incompleto no se pronostica ni se rellena: se cuenta por motivo (P3)."""
    validar_parametros(params)
    motivos: Counter[str] = Counter()
    pendientes: list[ContextoKronos] = []
    existentes = 0
    for dia in sorted(fechas):
        as_of = _medianoche(dia)
        for symbol in simbolos:
            if (symbol, as_of) in ya_hechos:
                existentes += 1
                continue
            ctx = construir_contexto(symbol, velas_1h.get(symbol, ()), as_of=as_of, lookback=params.lookback, horizon=params.horizon)
            if isinstance(ctx, MotivoSinContexto):
                motivos[ctx.value] += 1
            else:
                pendientes.append(ctx)

    inicio = reloj()
    hechos = 0
    detenido = False
    for ctx in pendientes:
        if max_segundos is not None and reloj() - inicio >= max_segundos:
            detenido = True
            break
        cierres = predictor.cierres_finales([ctx], params)
        if len(cierres) != 1:
            raise RuntimeError("El predictor devolvió un número de resultados distinto al de contextos.")
        guardar(ctx, resumir_trayectorias(ctx.ultimo_cierre, cierres[0]), cierres[0])
        hechos += 1
        if progreso is not None:
            progreso(hechos, len(pendientes), reloj() - inicio)
    return ResultadoGeneracion(
        generados=hechos,
        ya_existian=existentes,
        pendientes=len(pendientes) - hechos,
        sin_contexto=dict(motivos),
        detenido_por_tiempo=detenido,
        segundos=reloj() - inicio,
    )


# --- Observaciones para evaluar ------------------------------------------------------

@dataclass(frozen=True)
class PronosticoGuardado:
    """Lo mínimo de un pronóstico guardado que necesita la evaluación."""

    symbol: str
    as_of: datetime
    last_close: Decimal
    expected_return: Decimal


def _momentum(cierres: Mapping[datetime, Decimal], entrada: datetime, horas: int) -> Decimal | None:
    ahora, antes = cierres.get(entrada), cierres.get(entrada - UNA_HORA * horas)
    if ahora is None or antes is None or antes <= 0:
        return None
    return ahora / antes - 1


def construir_observaciones_k1(
    pronosticos: Sequence[PronosticoGuardado],
    diarias_por_simbolo: Mapping[str, list[Candle]],
    velas_1h_por_simbolo: Mapping[str, list[Candle]],
) -> tuple[list[ObservacionK1], dict[str, int]]:
    """Une cada pronóstico con su desenlace simulado y las líneas base. Devuelve las
    observaciones completas y el CONTEO de descartes por motivo (nada se rellena):

    - `sin_observacion_del_arnes`: el arnés no genera observación ese día (score no
      calculable por calentamiento, o sin vela de entrada);
    - `censurada`: la posición simulada sigue abierta al final de los datos;
    - `sin_momentum`: falta historia de 1 h para el momentum de 24 h o 7 d;
    - `cierre_inconsistente`: el último cierre del contexto no coincide con la vela
      de entrada (señal de datos que cambiaron entre la generación y la evaluación).
    """
    descartes: Counter[str] = Counter()
    por_simbolo: dict[str, dict[date, object]] = {}
    cierres_por_simbolo: dict[str, dict[datetime, Decimal]] = {}
    observaciones: list[ObservacionK1] = []
    for p in pronosticos:
        s = p.symbol
        if s not in por_simbolo:
            velas = velas_1h_por_simbolo.get(s, [])
            por_simbolo[s] = {o.dia: o for o in observaciones_de_simbolo(s, list(diarias_por_simbolo.get(s, [])), list(velas))}
            cierres_por_simbolo[s] = {v["open_time"]: v["close"] for v in velas}
        dia = p.as_of.astimezone(timezone.utc).date()
        ob = por_simbolo[s].get(dia)
        if ob is None:
            descartes["sin_observacion_del_arnes"] += 1
            continue
        if ob.retorno_neto is None:
            descartes["censurada"] += 1
            continue
        entrada = p.as_of - UNA_HORA
        cierres = cierres_por_simbolo[s]
        if cierres.get(entrada) != p.last_close:
            descartes["cierre_inconsistente"] += 1
            continue
        m24, m7 = _momentum(cierres, entrada, 24), _momentum(cierres, entrada, 168)
        if m24 is None or m7 is None:
            descartes["sin_momentum"] += 1
            continue
        observaciones.append(
            ObservacionK1(
                symbol=s, dia=dia, retorno_neto=ob.retorno_neto, kronos=p.expected_return,
                score_v1=ob.score, mom_24h=m24, mom_7d=m7,
            )
        )
    return observaciones, dict(descartes)
