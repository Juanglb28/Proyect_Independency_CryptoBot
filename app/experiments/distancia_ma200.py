"""D1 — distancia del precio a la media de 200 periodos (4 h) en BTCUSDT
(protocolo `docs/protocolo-distancia-ma200-4h.md`, v1).

Este módulo junta las reglas puras del dominio con la entrada y la salida del estudio,
SIN base de datos ni red: las velas le llegan por argumento (P2). Solo lectura: no
escribe nada en ningún lado salvo lo que haga el script que lo llama (un JSON en
`data/distancia_ma200/`).

- Modo conteo (`solo_contar`): cobertura y control de agregación. NO calcula ninguna
  distancia ni ningún retorno.
- Evaluación: primero se exige el control 1 (la vela de 4 h agregada coincide exactamente
  con la guardada). Si falla, no se calcula nada más: se corrige antes de leer resultados.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.data.exchange_client import Candle
from app.domain.rules.distancia_ma import (
    HORIZONTE_PRIMARIO,
    UMBRAL_ALTO,
    UMBRAL_BAJO,
    ConteoAgregacion,
    Contraste,
    ControlAgregacion,
    Distribucion,
    Episodio,
    InformeD1,
    ResumenCobertura,
    Vela1h,
    Vela4h,
    agregar_a_4h,
    comparar_con_4h_guardadas,
    contar_cobertura,
    evaluar_d1,
)
from app.experiments.sombra_ia import verificar_protocolo as _verificar

SIMBOLO = "BTCUSDT"
RUTA_PROTOCOLO = Path("docs/protocolo-distancia-ma200-4h.md")
RUTA_HUELLA = Path("data/distancia_ma200/protocolo_sha256.txt")


def verificar_protocolo(ruta_protocolo: Path = RUTA_PROTOCOLO, ruta_huella: Path = RUTA_HUELLA) -> str:
    """Devuelve la huella si coincide con la registrada; si no, lanza `ProtocoloModificado`."""
    return _verificar(ruta_protocolo, ruta_huella)


def a_vela_1h(c: Candle) -> Vela1h:
    return Vela1h(c["open_time"], c["open"], c["high"], c["low"], c["close"])


def a_vela_4h(c: Candle) -> Vela4h:
    return Vela4h(c["open_time"], c["open"], c["high"], c["low"], c["close"])


@dataclasses.dataclass(frozen=True)
class ResultadoCorrida:
    agregacion: ConteoAgregacion
    cobertura: ResumenCobertura
    control: ControlAgregacion
    informe: InformeD1 | None
    evaluada: bool
    motivo_sin_evaluar: str | None


def correr(
    velas_1h: Sequence[Candle], velas_4h_guardadas: Sequence[Candle], *, solo_contar: bool = False
) -> ResultadoCorrida:
    agregadas, conteo = agregar_a_4h([a_vela_1h(c) for c in velas_1h])
    control = comparar_con_4h_guardadas(agregadas, [a_vela_4h(c) for c in velas_4h_guardadas])
    cobertura = contar_cobertura(agregadas)
    if solo_contar:
        return ResultadoCorrida(conteo, cobertura, control, None, False, None)
    if not control.valido:
        return ResultadoCorrida(
            conteo, cobertura, control, None, False,
            "el control de agregación FALLÓ: el estudio no es válido; se corrige antes de leer nada",
        )
    if cobertura.observaciones == 0:
        return ResultadoCorrida(conteo, cobertura, control, None, False, "no hay observaciones elegibles (menos de 600 velas continuas)")
    return ResultadoCorrida(conteo, cobertura, control, evaluar_d1(agregadas), True, None)


# --- Presentación -----------------------------------------------------------------------------


def _pct(x: Decimal) -> str:
    return f"{x * 100:+.1f} %"


def _pp(x: Decimal) -> str:
    return f"{x * 100:+.2f} pp"


def _fecha(t: datetime | None) -> str:
    return "—" if t is None else t.strftime("%Y-%m-%d %H:%M")


def formatear_cobertura(conteo: ConteoAgregacion, cob: ResumenCobertura, control: ControlAgregacion) -> list[str]:
    lineas = [
        f"Velas de 1 h cerradas de {SIMBOLO}: {conteo.velas_1h} · bloques de 4 h completos: {conteo.bloques_completos} · incompletos (no se rellenan): {conteo.bloques_incompletos}",
        f"Serie de 4 h: {cob.velas_4h} velas en {cob.tramos} tramo(s) continuo(s) · de {_fecha(cob.primera)} a {_fecha(cob.ultima)} UTC",
        f"Observaciones elegibles (vela 600 de cada tramo en adelante): {cob.observaciones} · desde {_fecha(cob.primera_elegible)} UTC",
        f"Observaciones clasificables (tras la calentada del percentil de 1.080): {cob.clasificadas}",
    ]
    if control.comparadas == 0:
        lineas.append("CONTROL 1 (agregación vs 4 h guardadas): no hay velas de 4 h guardadas que comparar → sin control.")
    elif control.valido:
        lineas.append(
            f"CONTROL 1 (agregación vs 4 h guardadas): CUMPLIDO — {control.coinciden} de {control.comparadas} velas coinciden exactamente "
            f"(O/H/L/C); {control.solo_guardadas} guardadas sin equivalente agregado."
        )
    else:
        lineas.append(
            f"CONTROL 1 (agregación vs 4 h guardadas): FALLÓ — {control.n_discrepancias} de {control.comparadas} velas difieren."
        )
        lineas += [f"  {t.strftime('%Y-%m-%d %H:%M')} UTC: difiere {campos}" for t, campos in control.discrepancias]
    lineas.append(
        "CONTROL 2 (media vs tabla `indicadores`): no hay control cruzado — la tabla solo guarda SMA 90/120/180; "
        "la SMA se verifica contra `indicator_calculator.sma` en las pruebas unitarias."
    )
    return lineas


def _linea_distribucion(etiqueta: str, d: Distribucion | None) -> str:
    if d is None:
        return f"  {etiqueta}: sin datos"
    return (
        f"  {etiqueta:<6} n={d.n:<6} mín {_pct(d.minimo)} · P1 {_pct(d.p1)} · P5 {_pct(d.p5)} · P25 {_pct(d.p25)} · "
        f"P50 {_pct(d.p50)} · P75 {_pct(d.p75)} · P95 {_pct(d.p95)} · P99 {_pct(d.p99)} · máx {_pct(d.maximo)}"
    )


def _episodios(titulo: str, episodios: Sequence[Episodio], maximo: int = 40) -> list[str]:
    lineas = [f"  {titulo}: {len(episodios)} episodio(s)"]
    for e in list(episodios)[:maximo]:
        lineas.append(f"    {_fecha(e.inicio)} → {_fecha(e.fin)} UTC · {e.velas} velas · extremo {_pct(e.extremo)}")
    if len(episodios) > maximo:
        lineas.append(f"    … y {len(episodios) - maximo} más (todos en el JSON)")
    return lineas


def _linea_contraste(c: Contraste) -> str:
    ic = (
        "no calculable"
        if c.intervalo is None
        else f"{_pp(c.intervalo.media)} [{_pp(c.intervalo.ci_inferior)}, {_pp(c.intervalo.ci_superior)}]"
    )
    muestra = "" if c.muestra == "TODA" else f" · {c.muestra}"
    return (
        f"  {c.ma:<11} {c.cola.value:<9} h={c.horizonte:<2}{muestra}: velas cola {c.velas_cola} / centro {c.velas_centro} · "
        f"días {c.dias} · episodios {c.episodios} → dif {ic} → {c.veredicto.value}"
    )


def formatear_informe(r: ResultadoCorrida, *, titulo: str) -> list[str]:
    inf = r.informe
    assert inf is not None
    lineas = [titulo, ""] + formatear_cobertura(r.agregacion, r.cobertura, r.control)
    lineas += ["", "PARTE A — DESCRIPCIÓN (SMA 200 de 4 h; distancia = cierre / media − 1)"]
    lineas.append(_linea_distribucion("toda", inf.dist_sma))
    lineas += [_linea_distribucion(str(a), d) for a, d in inf.dist_sma_por_anio.items()]
    lineas.append("  (EMA 200, comprobación)")
    lineas.append(_linea_distribucion("toda", inf.dist_ema))
    lineas += [_linea_distribucion(str(a), d) for a, d in inf.dist_ema_por_anio.items()]
    rp = inf.rupturas
    pct_alto = Decimal(rp.velas_altas) / rp.velas_total * 100 if rp.velas_total else Decimal(0)
    pct_bajo = Decimal(rp.velas_bajas) / rp.velas_total * 100 if rp.velas_total else Decimal(0)
    lineas += [
        "",
        f"Umbrales del usuario sobre la SMA: velas con dist > {_pct(UMBRAL_ALTO)}: {rp.velas_altas} ({pct_alto:.2f} % de {rp.velas_total}) · "
        f"velas con dist < {_pct(UMBRAL_BAJO)}: {rp.velas_bajas} ({pct_bajo:.2f} %)",
    ]
    lineas += _episodios(f"por encima de {_pct(UMBRAL_ALTO)}", rp.episodios_altos)
    lineas += _episodios(f"por debajo de {_pct(UMBRAL_BAJO)}", rp.episodios_bajos)
    v = inf.ventanas
    if v.ventanas:
        lineas.append(
            f"Ventanas móviles de 365 días (paso de 1 día): {v.ventanas} · sin ninguna ruptura de [{_pct(UMBRAL_BAJO)}, {_pct(UMBRAL_ALTO)}]: "
            f"{v.sin_ruptura} ({float(v.fraccion_sin_ruptura) * 100:.1f} %) · días con alguna ruptura: {len(v.dias_con_ruptura)}"
        )
    else:
        lineas.append("Ventanas móviles de 365 días: la muestra es más corta que un año; no hay ventanas completas.")
    lineas += [
        "",
        f"PARTE B — PREDICCIÓN · velas clasificadas: {inf.n_clasificadas} · retorno primario a {HORIZONTE_PRIMARIO} velas (3 días) · brutos, sin costos",
        "Hipótesis: tras COLA_BAJA el retorno es MAYOR que tras CENTRO; tras COLA_ALTA, MENOR.",
        "CONTRASTES PRIMARIOS (deciden):",
    ]
    lineas += [_linea_contraste(c) for c in inf.primarios]
    lineas += ["", "SECUNDARIOS (exploratorios; no deciden; sin corrección por comparaciones múltiples):"]
    lineas += [_linea_contraste(c) for c in inf.secundarios]
    lineas += [
        "",
        "Nota: «UMBRAL_FIJO» usa +19 % / −25 %, valores vistos en el gráfico antes del estudio → solo descriptivo.",
        f"Corte de las mitades: {_fecha(inf.fecha_de_corte_mitades)} UTC",
    ]
    return lineas


def informe_a_json(r: ResultadoCorrida, *, huella: str) -> dict:
    inf = r.informe
    assert inf is not None

    def num(x):
        return None if x is None else str(x)

    def dist(d):
        return None if d is None else {k: (v if isinstance(v, int) else str(v)) for k, v in dataclasses.asdict(d).items()}

    def epis(e: Episodio):
        return {"inicio": e.inicio.isoformat(), "fin": e.fin.isoformat(), "velas": e.velas, "extremo": str(e.extremo)}

    def contraste(c: Contraste):
        i = c.intervalo
        return {
            "ma": c.ma, "cola": c.cola.value, "horizonte": c.horizonte, "muestra": c.muestra,
            "velas_cola": c.velas_cola, "velas_centro": c.velas_centro, "dias": c.dias, "episodios": c.episodios,
            "intervalo": None if i is None else {"media": str(i.media), "ci_inferior": str(i.ci_inferior), "ci_superior": str(i.ci_superior), "n_dias": i.n},
            "veredicto": c.veredicto.value,
        }

    cob = r.cobertura
    return {
        "protocolo_sha256": huella,
        "simbolo": SIMBOLO,
        "cobertura": {
            "velas_1h": r.agregacion.velas_1h, "bloques_incompletos": r.agregacion.bloques_incompletos,
            "velas_4h": cob.velas_4h, "tramos": cob.tramos, "observaciones": cob.observaciones, "clasificadas": cob.clasificadas,
            "primera": cob.primera.isoformat() if cob.primera else None, "ultima": cob.ultima.isoformat() if cob.ultima else None,
        },
        "control_agregacion": {
            "comparadas": r.control.comparadas, "coinciden": r.control.coinciden, "valido": r.control.valido,
        },
        "distribucion_sma": dist(inf.dist_sma),
        "distribucion_sma_por_anio": {str(a): dist(d) for a, d in inf.dist_sma_por_anio.items()},
        "distribucion_ema": dist(inf.dist_ema),
        "distribucion_ema_por_anio": {str(a): dist(d) for a, d in inf.dist_ema_por_anio.items()},
        "umbrales": {
            "velas_altas": inf.rupturas.velas_altas, "velas_bajas": inf.rupturas.velas_bajas, "velas_total": inf.rupturas.velas_total,
            "episodios_altos": [epis(e) for e in inf.rupturas.episodios_altos],
            "episodios_bajos": [epis(e) for e in inf.rupturas.episodios_bajos],
        },
        "ventanas_anuales": {
            "ventanas": inf.ventanas.ventanas, "sin_ruptura": inf.ventanas.sin_ruptura,
            "dias_con_ruptura": [d.isoformat() for d in inf.ventanas.dias_con_ruptura],
        },
        "primarios": [contraste(c) for c in inf.primarios],
        "secundarios": [contraste(c) for c in inf.secundarios],
        "corte_mitades": inf.fecha_de_corte_mitades.isoformat() if inf.fecha_de_corte_mitades else None,
        "clasificadas": inf.n_clasificadas,
    }
