"""S1 — métrica a la sombra del análisis de IA (protocolo `docs/protocolo-metrica-sombra-ia.md`).

Este módulo junta lo ya decidido por la IA (unidades base) con el desenlace simulado de
cada candidato, SIN base de datos ni red: las velas le llegan por argumento (P2). Usa las
mismas reglas de salida del motor paper que el resto de los estudios
(`simular_salida`, `retorno_marcado_a_mercado`) y no modifica nada.

- Entrada: cierre de la última vela de 1 h cerrada en o antes del inicio del ciclo
  (misma regla que `observaciones_de_simbolo`). Solo velas POSTERIORES a la entrada
  determinan el desenlace: nada futuro entra a la decisión.
- Salida resuelta → `retorno_neto` real. Salida aún abierta → valorada a mercado al cierre
  de la última vela cerrada disponible (mismo corte para todas).
- Sin velas de entrada o posteriores → la unidad se DESCARTA y se cuenta (nada se rellena).

TODO — DECISIÓN PENDIENTE (no resuelta aquí, a propósito): distinguir «omitido» de
«excluido por calidad de datos» exige guardar la decisión por candidato; es otra
decisión del usuario y no forma parte de este protocolo.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.data.exchange_client import Candle
from app.data.exit_simulation import retorno_marcado_a_mercado, simular_salida
from app.domain.rules.sombra_ia import (
    ConteoMuestra,
    InformeS1,
    UnidadBase,
    UnidadValorada,
    contar_muestra,
    evaluar_s1,
)

UNA_HORA = timedelta(hours=1)
RUTA_PROTOCOLO = Path("docs/protocolo-metrica-sombra-ia.md")
RUTA_HUELLA = Path("data/sombra_ia/protocolo_sha256.txt")


# --- Huella del protocolo --------------------------------------------------------------------


def huella_protocolo(ruta: Path = RUTA_PROTOCOLO) -> str:
    """SHA-256 del protocolo con saltos de línea normalizados (igual que K1)."""
    return hashlib.sha256(ruta.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


class ProtocoloModificado(RuntimeError):
    """El texto del protocolo ya no coincide con la huella registrada."""


def verificar_protocolo(ruta_protocolo: Path = RUTA_PROTOCOLO, ruta_huella: Path = RUTA_HUELLA) -> str:
    """Devuelve la huella si coincide con la registrada; si no, lanza `ProtocoloModificado`.
    Cambiar el texto tras congelarlo es otro experimento, no un ajuste."""
    actual = huella_protocolo(ruta_protocolo)
    registrada = ruta_huella.read_text(encoding="utf-8").strip().lower()
    if actual != registrada:
        raise ProtocoloModificado(
            f"El protocolo cambió desde que se congeló (huella actual {actual[:12]}…, registrada {registrada[:12]}…). "
            "Cualquier cambio es un experimento NUEVO: no se evalúa con este protocolo."
        )
    return actual


# --- Desenlaces -----------------------------------------------------------------------------


def _inicio_de_hora(t: datetime) -> datetime:
    t = t.astimezone(timezone.utc)
    return t.replace(minute=0, second=0, microsecond=0)


def valorar_unidades(
    bases: Sequence[UnidadBase], velas_1h_por_simbolo: Mapping[str, Sequence[Candle]]
) -> tuple[list[UnidadValorada], dict[str, int]]:
    """Asigna a cada unidad su desenlace simulado. Devuelve las unidades valoradas y el
    CONTEO de descartes por motivo:

    - `sin_vela_de_entrada`: no existe la vela de 1 h cerrada previa al inicio del ciclo;
    - `sin_velas_posteriores`: no hay ninguna vela cerrada después de la de entrada.
    """
    descartes: Counter[str] = Counter()
    indices: dict[str, dict[datetime, int]] = {}
    listas: dict[str, list[Candle]] = {}
    valoradas: list[UnidadValorada] = []
    for u in bases:
        if u.symbol not in listas:
            listas[u.symbol] = list(velas_1h_por_simbolo.get(u.symbol, []))
            indices[u.symbol] = {v["open_time"].astimezone(timezone.utc): i for i, v in enumerate(listas[u.symbol])}
        apertura_entrada = _inicio_de_hora(u.started_at) - UNA_HORA
        idx = indices[u.symbol].get(apertura_entrada)
        if idx is None:
            descartes["sin_vela_de_entrada"] += 1
            continue
        velas = listas[u.symbol]
        mtm = retorno_marcado_a_mercado(velas, idx)
        if mtm is None:
            descartes["sin_velas_posteriores"] += 1
            continue
        salida = simular_salida(velas, idx)
        valoradas.append(
            UnidadValorada(
                **{f.name: getattr(u, f.name) for f in dataclasses.fields(UnidadBase)},
                retorno_neto=salida.retorno_neto if salida is not None else mtm,
                resuelta=salida is not None,
            )
        )
    return valoradas, dict(descartes)


# --- Corrida (sin espiar) ---------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ResultadoCorrida:
    conteo: ConteoMuestra
    evaluada: bool
    informe: InformeS1 | None
    descartes: dict[str, int]


def correr(bases: Sequence[UnidadBase], cargar_velas) -> ResultadoCorrida:
    """Modo conteo / evaluación del protocolo. `cargar_velas(simbolos) -> {simbolo: [velas 1h]}`
    SOLO se invoca si los tres mínimos de muestra se cumplen con las decisiones guardadas:
    mientras no, no se mira ni una vela posterior ni se calcula ningún desenlace."""
    conteo = contar_muestra(bases)
    if not conteo.suficiente:
        return ResultadoCorrida(conteo, False, None, {})
    simbolos = sorted({u.symbol for u in bases})
    valoradas, descartes = valorar_unidades(bases, cargar_velas(simbolos))
    return ResultadoCorrida(conteo, True, evaluar_s1(valoradas), descartes)


# --- Presentación -----------------------------------------------------------------------------


def _fmt_pct(x: Decimal) -> str:
    return f"{x * 100:+.2f} pp"


def _fmt_intervalo(i, fmt=_fmt_pct) -> str:
    if i is None:
        return "no calculable con esta muestra"
    return f"{fmt(i.media)} [{fmt(i.ci_inferior)}, {fmt(i.ci_superior)}] (n={i.n} días)"


def _fmt_ic(x: Decimal) -> str:
    return f"{x:+.3f}"


def _fmt_puntos(x: Decimal) -> str:
    return f"{x:+.2f} pts"


def formatear_conteo(c: ConteoMuestra) -> list[str]:
    lineas = [
        f"Ciclos con Consolidador y candidatos: {c.ciclos} (de ellos, «no_invertir»: {c.ciclos_no_invertir})",
        f"Ciclos pareados (con incluidos y no incluidos): {c.ciclos_pareados} · días con ciclo pareado: {c.dias_pareados}",
        f"Candidatos incluidos por la IA: {c.incluidos} · no incluidos: {c.no_incluidos} · por nivel: {c.por_nivel}",
    ]
    if c.suficiente:
        lineas.append("Mínimos del protocolo: CUMPLIDOS.")
    else:
        lineas.append("Mínimos del protocolo: NO cumplidos todavía → no se calcula ningún desenlace.")
        lineas += [f"  falta — {f}" for f in c.faltan]
    return lineas


def formatear_informe(informe: InformeS1, descartes: Mapping[str, int], *, titulo: str) -> list[str]:
    lineas = [titulo, ""]
    lineas += formatear_conteo(informe.conteo)
    if descartes:
        lineas.append(f"Unidades descartadas por falta de velas: {dict(descartes)}")
    lineas += [
        "",
        f"VEREDICTO: {informe.veredicto.value}",
        f"Contraste primario (incluidos − no incluidos, pareado por ciclo): {_fmt_intervalo(informe.primario)}",
    ]
    lineas += [f"  nota: {n}" for n in informe.notas]
    lineas += ["", "Secundarios (exploratorios; no deciden):"]
    for s in informe.secundarios:
        if s.intervalo is None and s.detalle:
            valor = "—"
        elif s.clave in ("escala_ordenada", "puntaje_dentro_de_incluidos"):
            valor = _fmt_intervalo(s.intervalo, _fmt_ic)
        elif s.clave == "confusion_prefiltro":
            valor = _fmt_intervalo(s.intervalo, _fmt_puntos)
        else:
            valor = _fmt_intervalo(s.intervalo)
        lineas.append(f"- {s.descripcion}: {valor}")
        lineas += [f"    {k}: {v}" for k, v in s.detalle.items()]
    return lineas


def informe_a_json(informe: InformeS1, descartes: Mapping[str, int], *, huella: str) -> dict:
    def iv(i):
        return None if i is None else {"n_dias": i.n, "media": str(i.media), "ci_inferior": str(i.ci_inferior), "ci_superior": str(i.ci_superior)}

    c = informe.conteo
    return {
        "protocolo_sha256": huella,
        "veredicto": informe.veredicto.value,
        "primario": iv(informe.primario),
        "conteo": {
            "ciclos": c.ciclos, "ciclos_no_invertir": c.ciclos_no_invertir, "ciclos_pareados": c.ciclos_pareados,
            "dias_pareados": c.dias_pareados, "incluidos": c.incluidos, "no_incluidos": c.no_incluidos,
            "por_nivel": c.por_nivel,
        },
        "descartes": dict(descartes),
        "secundarios": {s.clave: {"descripcion": s.descripcion, "intervalo": iv(s.intervalo), "detalle": s.detalle} for s in informe.secundarios},
        "notas": list(informe.notas),
    }
