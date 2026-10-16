"""Texto y JSON del informe de K1. Solo formatea lo que calcula `kronos_k1`; no calcula nada."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from app.domain.rules.kronos_k1 import K1_BLOQUE_FECHAS, SENALES, InformeK1, IntervaloBootstrap, ResultadoTerciles, VeredictoK1

_NOMBRES = {
    "kronos": "Kronos (retorno esperado)",
    "score_v1": "Score determinista v1",
    "mom_24h": "Momentum 24 h",
    "mom_7d": "Momentum 7 d",
}


def _f(x: Decimal | None, nd: int = 4) -> str:
    return "n/d" if x is None else f"{x:+.{nd}f}"


def _ic(i: IntervaloBootstrap | None) -> str:
    if i is None:
        return "n/d"
    if i.n <= K1_BLOQUE_FECHAS:
        # con una sola fecha-bloque posible todos los remuestreos son iguales: el «intervalo»
        # sería un punto y parecería precisión que no existe. Solo cambia el texto mostrado.
        return f"{_f(i.media)} [IC95 no estimable con n <= {K1_BLOQUE_FECHAS} fechas] (n={i.n} fechas)"
    return f"{_f(i.media)} [IC95 {_f(i.ci_inferior)}, {_f(i.ci_superior)}] (n={i.n} fechas)"


def _pct(x: Decimal | None) -> str:
    return "n/d" if x is None else f"{x * 100:+.2f}%"


def _terciles(t: ResultadoTerciles | None) -> str:
    if t is None:
        return "n/d (menos de 3 observaciones)"
    extra = f" · {t.estratos_excluidos} mes(es) excluido(s)" if t.estratos_excluidos else ""
    return (
        f"alto {_pct(t.alta)} − bajo {_pct(t.baja)} = {_pct(t.diferencia)} "
        f"[IC95 {_pct(t.ci_inferior)}, {_pct(t.ci_superior)}] (n={t.n}, k={t.k}){extra}"
    )


def formatear_informe_k1(
    titulo: str, informe: InformeK1, descartes: dict[str, int], *, decisivo: bool
) -> list[str]:
    """`decisivo=False` para la sensibilidad y el diagnóstico pre-corte: se muestran
    con la misma maquinaria pero su veredicto NO es el del protocolo."""
    l = [
        f"=== {titulo} ===",
        f"Observaciones resueltas: {informe.n_observaciones} · fechas comparables: {informe.n_fechas} "
        f"(excluidas por tener < 10 monedas o correlación indefinida: {informe.fechas_excluidas})",
    ]
    if descartes:
        l.append("Descartadas (nada se rellena): " + ", ".join(f"{k}={v}" for k, v in sorted(descartes.items())))
    if informe.ic:
        l.append("RankIC diario promedio (misma fechas y monedas para todas las señales):")
        for s in SENALES:
            marca = "  ← CONTRASTE PRIMARIO" if (s == "kronos" and decisivo) else ""
            l.append(f"  {_NOMBRES[s]:28s} {_ic(informe.ic[s])}{marca}")
        l.append(f"Diferencia pareada Kronos − momentum 24 h: {_ic(informe.dif_vs_mom_24h)}")
        l.append(f"Diferencia pareada Kronos − momentum 7 d:  {_ic(informe.dif_vs_mom_7d)}")
        l.append(f"Permutación (p, una cola): {informe.p_permutacion}")
        l.append(f"Correlación de rangos Kronos↔score v1 (promedio diario): {_f(informe.corr_kronos_score)}")
        l.append(f"Terciles de §8.7 (exploratorio):  {_terciles(informe.terciles)}")
        l.append(f"Terciles E4, centrado por mes:    {_terciles(informe.terciles_e4)}")
    for n in informe.notas:
        l.append(f"Nota: {n}")
    if decisivo:
        l.append(f"VEREDICTO DEL PROTOCOLO: {informe.veredicto.value}")
        if informe.veredicto is VeredictoK1.DISCRIMINA_Y_ANADE:
            l.append("Alcance: solo justifica pasar a K2 (sombra hacia delante). No se usa en compras, score ni pre-filtro.")
    else:
        l.append("(informativo: no es el veredicto del protocolo)")
    return l


def informe_a_json(x):
    """Convierte el informe (dataclasses, Decimal, enums) a algo serializable."""
    if isinstance(x, Decimal):
        return str(x)
    if isinstance(x, VeredictoK1):
        return x.value
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return {k: informe_a_json(v) for k, v in dataclasses.asdict(x).items()}
    if isinstance(x, dict):
        return {str(k): informe_a_json(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [informe_a_json(v) for v in x]
    return x
