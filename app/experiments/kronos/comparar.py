"""Fiabilidad entre semillas de dos corridas de K0 (enmienda E6).

Mide si el RANKING de monedas que produce Kronos es estable entre dos muestreos
distintos del mismo contexto: si dos corridas sobre las mismas monedas y la misma
fecha apenas coinciden, la señal con ese número de trayectorias es sobre todo
ruido de muestreo y no hay nada que contrastar. NO mira el retorno realizado: no
dice nada sobre capacidad predictiva, solo sobre estabilidad (K0).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from app.domain.rules.kronos_signal import spearman

_CERO = Decimal(0)


@dataclass(frozen=True)
class ComparacionCorridas:
    pares: int  # pronósticos presentes en ambas corridas
    solo_en_una: int
    spearman_retorno_esperado: Decimal | None
    spearman_prob_up: Decimal | None
    mismo_signo: Decimal | None  # fracción de pares con el mismo signo de retorno esperado
    diferencia_absoluta_media: Decimal | None
    desv_entre_monedas: Decimal | None  # desviación estándar del retorno esperado entre monedas (corrida A)
    error_estandar_medio: Decimal | None  # dispersión media / raíz(n trayectorias): ruido de muestreo de una media


def _clave(p: Mapping) -> tuple[str, str]:
    return p["symbol"], p["as_of"]


def _desv(valores: list[Decimal]) -> Decimal | None:
    if len(valores) < 2:
        return None
    m = sum(valores, _CERO) / len(valores)
    return (sum(((v - m) ** 2 for v in valores), _CERO) / (len(valores) - 1)).sqrt()


def comparar_corridas(a: Mapping, b: Mapping) -> ComparacionCorridas:
    pa = {_clave(p): p for p in a["pronosticos"]}
    pb = {_clave(p): p for p in b["pronosticos"]}
    comunes = sorted(pa.keys() & pb.keys())
    solo = len(pa.keys() ^ pb.keys())
    if not comunes:
        return ComparacionCorridas(0, solo, None, None, None, None, None, None)

    ea = [Decimal(pa[k]["resumen"]["expected_return"]) for k in comunes]
    eb = [Decimal(pb[k]["resumen"]["expected_return"]) for k in comunes]
    ua = [Decimal(pa[k]["resumen"]["prob_up"]) for k in comunes]
    ub = [Decimal(pb[k]["resumen"]["prob_up"]) for k in comunes]
    n = len(comunes)
    mismo = sum(1 for x, y in zip(ea, eb) if (x > 0) == (y > 0))
    dif = sum((abs(x - y) for x, y in zip(ea, eb)), _CERO) / n

    dispersiones = [
        Decimal(pa[k]["resumen"]["dispersion"]) for k in comunes if pa[k]["resumen"]["dispersion"] is not None
    ]
    n_traj = {int(pa[k]["resumen"]["n"]) for k in comunes}
    error = None
    if dispersiones and len(n_traj) == 1:
        error = (sum(dispersiones, _CERO) / len(dispersiones)) / Decimal(n_traj.pop()).sqrt()

    return ComparacionCorridas(
        pares=n,
        solo_en_una=solo,
        spearman_retorno_esperado=spearman(ea, eb),
        spearman_prob_up=spearman(ua, ub),
        mismo_signo=Decimal(mismo) / n,
        diferencia_absoluta_media=dif,
        desv_entre_monedas=_desv(ea),
        error_estandar_medio=error,
    )


def formatear_comparacion(c: ComparacionCorridas) -> list[str]:
    def f(x: Decimal | None, nd: int = 4) -> str:
        return "n/d" if x is None else f"{x:.{nd}f}"

    l = [
        "FIABILIDAD ENTRE SEMILLAS (sin mirar el retorno realizado).",
        f"Pronósticos comparables: {c.pares} · presentes en una sola corrida: {c.solo_en_una}.",
        f"Correlación de rangos del retorno esperado entre corridas: {f(c.spearman_retorno_esperado)} "
        f"(1 = mismo ranking de monedas; 0 = sin relación). Fracción al alza: {f(c.spearman_prob_up)}.",
        f"Mismo signo del retorno esperado: {f(c.mismo_signo)} · diferencia absoluta media: {f(c.diferencia_absoluta_media, 6)}.",
        f"Desv. del retorno esperado entre monedas: {f(c.desv_entre_monedas, 6)} · "
        f"error estándar medio del muestreo por moneda: {f(c.error_estandar_medio, 6)}.",
        "Lectura: si el error de muestreo es comparable a la desviación entre monedas, el ranking es mayormente "
        "ruido con ese número de trayectorias. Esto es estabilidad, no capacidad predictiva.",
    ]
    return l
