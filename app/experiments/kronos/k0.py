"""K0 — viabilidad del experimento Kronos (enmienda E6).

Responde, con datos propios y SIN sacar conclusiones de rendimiento:
cuánto tarda un pronóstico en este equipo, si es reproducible con semilla, qué
fracción de contextos se descarta por falta de velas y, solo como chequeo de
cordura, si el pronóstico se comporta mejor o peor que "no cambia"
(persistencia). Los números de sanidad de K0 NO son evidencia de capacidad
predictiva: con unos cientos de puntos correlacionados no se puede concluir nada.

Todo lo que entra al pronóstico existía en `as_of` (ver `contexto.py`); el
retorno realizado se lee DESPUÉS y solo se usa para contrastar.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.data.exchange_client import Candle
from app.domain.rules.kronos_signal import (
    CORTE_PREENTRENAMIENTO,
    ParametrosKronos,
    PuntoContraste,
    RankICDiario,
    ResumenKronos,
    SanidadVsPersistencia,
    rank_ic_diario,
    resumir_trayectorias,
    sanidad_vs_persistencia,
    validar_parametros,
)
from app.experiments.kronos.contexto import ContextoKronos, MotivoSinContexto, construir_contexto
from app.experiments.kronos.predictor import PredictorDeTrayectorias


def instantes_de_muestra(desde: date, hasta: date, cantidad: int) -> list[datetime]:
    """`cantidad` instantes de decisión (00:00 UTC) repartidos de forma pareja y
    DETERMINISTA entre `desde` y `hasta` (ambos incluidos si caben). Sin azar:
    la misma muestra en cada corrida."""
    if cantidad <= 0:
        raise ValueError("cantidad debe ser positiva.")
    if hasta < desde:
        raise ValueError("hasta no puede ser anterior a desde.")
    dias = (hasta - desde).days + 1
    if cantidad >= dias:
        offsets = list(range(dias))
    elif cantidad == 1:
        offsets = [0]
    else:
        offsets = [round(i * (dias - 1) / (cantidad - 1)) for i in range(cantidad)]
    vistos: set[int] = set()
    salida: list[datetime] = []
    for o in offsets:
        if o in vistos:
            continue
        vistos.add(o)
        d = desde + timedelta(days=o)
        salida.append(datetime(d.year, d.month, d.day, tzinfo=timezone.utc))
    return salida


@dataclass(frozen=True)
class PronosticoK0:
    symbol: str
    as_of: datetime
    resumen: ResumenKronos
    retorno_realizado: Decimal | None  # None si aún no hay (o faltan) velas posteriores


@dataclass(frozen=True)
class ReporteK0:
    parametros: ParametrosKronos
    dispositivo: str
    contextos_pedidos: int
    contextos_utilizables: int
    motivos_sin_contexto: dict[str, int]
    sin_retorno_realizado: int
    anteriores_al_corte: int  # instantes < 2024-07-01: contaminados por el preentrenamiento
    segundos_totales: float
    segundos_por_pronostico: float | None
    reproducible: bool | None  # None si no se comprobó
    sanidad: SanidadVsPersistencia | None
    rank_ic: RankICDiario | None
    pronosticos: tuple[PronosticoK0, ...]


def retorno_realizado(velas: Sequence[Candle], ctx: ContextoKronos) -> Decimal | None:
    """Retorno entre el último cierre conocido y el cierre de la vela final del
    horizonte. `None` si esa vela no existe o no está cerrada: no se estima."""
    objetivo = ctx.apertura_de_la_vela_final
    for v in velas:
        if v["open_time"] == objetivo:
            if not v["cerrada"] or v["close"] <= 0:
                return None
            return v["close"] / ctx.ultimo_cierre - 1
    return None


def ejecutar_k0(
    velas_1h: Mapping[str, Sequence[Candle]],
    *,
    predictor: PredictorDeTrayectorias,
    params: ParametrosKronos,
    instantes: Sequence[datetime],
    dispositivo: str,
    comprobar_reproducibilidad: bool = True,
    max_context: int = 512,
    reloj: Callable[[], float] = time.perf_counter,
    progreso: Callable[[int, int, float], None] | None = None,
) -> ReporteK0:
    """`progreso(hechos, total, segundos_transcurridos)` se llama tras cada pronóstico
    (solo informativo; no influye en nada)."""
    validar_parametros(params, max_context=max_context)

    motivos: Counter[str] = Counter()
    contextos: list[ContextoKronos] = []
    pedidos = 0
    for as_of in instantes:
        for symbol, velas in velas_1h.items():
            pedidos += 1
            resultado = construir_contexto(
                symbol, velas, as_of=as_of, lookback=params.lookback, horizon=params.horizon
            )
            if isinstance(resultado, MotivoSinContexto):
                motivos[resultado.value] += 1
            else:
                contextos.append(resultado)

    inicio = reloj()
    resumenes: list[ResumenKronos] = []
    for i, ctx in enumerate(contextos, start=1):
        cierres = predictor.cierres_finales([ctx], params)
        if len(cierres) != 1:
            raise RuntimeError("El predictor devolvió un número de resultados distinto al de contextos.")
        resumenes.append(resumir_trayectorias(ctx.ultimo_cierre, cierres[0]))
        if progreso is not None:
            progreso(i, len(contextos), reloj() - inicio)
    segundos = reloj() - inicio

    # Control de reproducibilidad: el PRIMER contexto, pedido de nuevo con otra
    # composición y otro orden (el último contexto va antes). Con semilla por
    # contexto el resultado debe ser idéntico; si cambia, el pronóstico dependería
    # de con quién se procese y no serviría para un experimento reproducible.
    reproducible: bool | None = None
    if comprobar_reproducibilidad and contextos:
        otro_orden = [contextos[-1], contextos[0]] if len(contextos) > 1 else [contextos[0]]
        otra_vez = predictor.cierres_finales(otro_orden, params)[-1]
        reproducible = resumir_trayectorias(contextos[0].ultimo_cierre, otra_vez) == resumenes[0]

    pronosticos = tuple(
        PronosticoK0(c.symbol, c.as_of, r, retorno_realizado(velas_1h[c.symbol], c))
        for c, r in zip(contextos, resumenes)
    )
    puntos = [
        PuntoContraste(p.symbol, p.as_of.date().isoformat(), p.resumen.expected_return, p.retorno_realizado)
        for p in pronosticos
        if p.retorno_realizado is not None
    ]
    return ReporteK0(
        parametros=params,
        dispositivo=dispositivo,
        contextos_pedidos=pedidos,
        contextos_utilizables=len(contextos),
        motivos_sin_contexto=dict(motivos),
        sin_retorno_realizado=len(pronosticos) - len(puntos),
        anteriores_al_corte=sum(1 for a in instantes if a < CORTE_PREENTRENAMIENTO),
        segundos_totales=segundos,
        segundos_por_pronostico=(segundos / len(contextos)) if contextos else None,
        reproducible=reproducible,
        sanidad=sanidad_vs_persistencia(puntos) if puntos else None,
        rank_ic=rank_ic_diario(puntos) if puntos else None,
        pronosticos=pronosticos,
    )


def formatear_reporte(r: ReporteK0) -> list[str]:
    """Texto del informe de K0, con sus limitaciones declaradas."""
    p = r.parametros
    l = [
        "K0 — VIABILIDAD DE KRONOS (enmienda E6). Sin conclusiones de rendimiento.",
        f"Modelo: {p.model_id} (rev {p.weights_revision[:10]}) · tokenizador {p.tokenizer_id} (rev {p.tokenizer_revision[:10]})",
        f"Código Kronos: commit {p.code_commit[:10]} · dispositivo: {r.dispositivo}",
        f"Lookback {p.lookback} velas de 1 h · horizonte {p.horizon} · trayectorias {p.n_trajectories} · "
        f"T {p.temperature} · top_p {p.top_p} · semilla {p.seed} · volumen {'sí' if p.usa_volumen else 'no (solo OHLC)'}",
        f"Contextos pedidos: {r.contextos_pedidos} · utilizables: {r.contextos_utilizables} · "
        f"sin retorno realizado: {r.sin_retorno_realizado}",
    ]
    if r.motivos_sin_contexto:
        l.append("Descartados por dato faltante (no se rellenó nada): " + ", ".join(
            f"{k}={v}" for k, v in sorted(r.motivos_sin_contexto.items())))
    if r.anteriores_al_corte:
        l.append(
            f"AVISO: {r.anteriores_al_corte} instante(s) anteriores a 2024-07-01. Kronos pudo ver esos datos en el "
            "preentrenamiento: sus resultados NO miden predicción."
        )
    if r.segundos_por_pronostico is not None:
        total = r.segundos_totales
        l.append(
            f"Tiempo: {total:.1f} s en total · {r.segundos_por_pronostico:.2f} s por pronóstico "
            f"({p.n_trajectories} trayectorias cada uno)."
        )
    if r.reproducible is not None:
        l.append("Reproducibilidad con la misma semilla: " + ("IDÉNTICA" if r.reproducible else "DIFERENTE (revisar)"))
    if r.sanidad is not None:
        s = r.sanidad
        l.append(
            f"Sanidad vs persistencia (n={s.n}): error absoluto medio Kronos {s.mae_kronos} vs persistencia "
            f"{s.mae_persistencia} → {'Kronos menor' if s.kronos_mejor_que_persistencia else 'persistencia menor o igual'}."
        )
        if s.acierto_direccion is not None:
            l.append(f"Acierto de dirección {s.acierto_direccion} (tasa base de alzas {s.tasa_base_alza}).")
    if r.rank_ic is not None:
        ic = r.rank_ic
        l.append(
            f"RankIC diario promedio: {ic.promedio if ic.promedio is not None else 'n/d'} "
            f"({ic.dias_usados} días usados, {ic.dias_excluidos} excluidos por tener menos de 10 monedas)."
        )
    l.append(
        "LIMITACIONES: estas cifras son un chequeo de cordura con pocas fechas y monedas correlacionadas; "
        "no prueban ni descartan capacidad predictiva. La evaluación formal es K1 (pre-registrada)."
    )
    return l
