"""Reglas puras del RESUMEN POR TELEGRAM de un ciclo de análisis (2026-10-11).

Decisión del usuario: recibir un mensaje al terminar cada ciclo de análisis,
con lo que pasó, para no tener que abrir la base ni los logs. Aquí solo se
ARMA el texto a partir de cifras ya calculadas y guardadas; no se lee la base,
no se llama a nadie y no se calcula nada nuevo (P1). Nada de esto es camino de
una orden (P2): un resumen es un aviso informativo y nunca cambia posiciones,
órdenes ni reglas de riesgo.

Lo que cuenta la IA (la razón del Consolidador) se copia tal cual, recortada:
es texto generado por un modelo y se muestra como cita, no como dato.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

_LARGO_MAX_RAZON = 300
_DOS_DECIMALES = Decimal("0.01")


class OrigenCiclo(str, Enum):
    """Quién lanzó el ciclo que se resume."""

    PROGRAMADO_DIARIO = "PROGRAMADO_DIARIO"  # el job de las 00:05 UTC
    CANDIDATOS_NUEVOS = "CANDIDATOS_NUEVOS"  # lo disparó el ciclo horario
    MANUAL = "MANUAL"  # `scripts/run_daily_cycle.py` (a mano o por la tarea de Windows)


_ETIQUETA_ORIGEN = {
    OrigenCiclo.PROGRAMADO_DIARIO: "programado 00:05 UTC",
    OrigenCiclo.CANDIDATOS_NUEVOS: "disparado por candidatos nuevos",
    OrigenCiclo.MANUAL: "manual",
}


@dataclass(frozen=True)
class PropuestaParaResumen:
    symbol: str
    puntaje_ponderado: Decimal
    tier_entrada: str | None  # "NORMAL" | "MAYOR_RIESGO" | "NINGUNO" | None
    confianza_global: str


@dataclass(frozen=True)
class EntradaParaResumen:
    """Qué pasó con un candidato al intentar abrirle una posición paper
    (apertura automática, 2026-10-12)."""

    symbol: str
    serie: str  # "ESTRATEGIA" | "OBSERVACION"
    puntaje: Decimal
    resultado: str  # ABIERTA | PENDIENTE_APROBACION | YA_PROCESADA | SIN_PRECIO | DESCARTADA
    motivos: tuple[str, ...] = ()


@dataclass(frozen=True)
class DatosResumenCiclo:
    origen: OrigenCiclo
    ciclo_id: uuid.UUID
    terminado_en: datetime
    candidatos_evaluados: tuple[str, ...]
    candidatos_excluidos_por_calidad: tuple[str, ...]
    propuestas: tuple[PropuestaParaResumen, ...]
    costo_ciclo_usd: Decimal
    gasto_hoy_usd: Decimal
    tope_usd: Decimal
    recomendacion: str | None = None
    razon_no_invertir: str | None = None
    # Solo si el usuario declaró su saldo de Anthropic; es una ESTIMACIÓN.
    saldo_estimado_usd: Decimal | None = None
    saldo_minimo_por_ciclo_usd: Decimal | None = None
    # Apertura automática de posiciones paper (2026-10-12). `entradas_informadas`
    # distingue "no se intentó / no se pudo" de "se intentó y no había nada".
    entradas: tuple[EntradaParaResumen, ...] = ()
    entradas_informadas: bool = False
    apertura_bloqueada_por_reconciliacion: bool = False
    apertura_fallo: bool = False


def _exigir_utc(momento: datetime) -> None:
    if momento.tzinfo is None or momento.utcoffset() != timedelta(0):
        raise ValueError("La fecha del resumen debe tener zona horaria UTC explícita (sección 15.3 del plan).")


def _usd(valor: Decimal) -> str:
    return f"{valor.quantize(_DOS_DECIMALES)}"


def _recortar(texto: str, largo: int) -> str:
    limpio = " ".join(texto.split())
    return limpio if len(limpio) <= largo else limpio[: largo - 1].rstrip() + "…"


def _texto_tier(tier: str | None) -> str:
    if tier == "NORMAL":
        return "supera el umbral de entrada"
    if tier == "MAYOR_RIESGO":
        return "entrada de MAYOR RIESGO (requiere tu aprobación)"
    return "no califica para entrada"


def _por_que_sin_propuestas(datos: DatosResumenCiclo) -> str:
    """Explica un 'sin propuestas' solo con lo que el resultado ya contiene."""
    if not datos.candidatos_evaluados:
        return "El pre-filtro no dejó ningún candidato."
    if datos.recomendacion is None:
        return "Todos los candidatos se excluyeron por calidad de datos insuficiente; no se llamó al Consolidador."
    if datos.recomendacion == "no_invertir":
        razon = f" Razón del Consolidador: «{_recortar(datos.razon_no_invertir, _LARGO_MAX_RAZON)}»" if datos.razon_no_invertir else ""
        return f"El Consolidador recomendó NO invertir.{razon}"
    return f"El Consolidador recomendó '{datos.recomendacion}' pero no se guardó ninguna propuesta (revisa los logs)."


_ETIQUETA_SERIE = {"ESTRATEGIA": "estrategia", "OBSERVACION": "observación"}
_ETIQUETA_RESULTADO = {
    "ABIERTA": "posición paper ABIERTA",
    "PENDIENTE_APROBACION": "pendiente de tu aprobación",
    "YA_PROCESADA": "ya procesada antes",
    "SIN_PRECIO": "sin precio disponible, no se abrió",
}


def _lineas_de_entradas(datos: DatosResumenCiclo) -> list[str]:
    if datos.apertura_fallo:
        return ["Posiciones paper: la apertura automática FALLÓ (revisa los logs); no se abrió nada."]
    if datos.apertura_bloqueada_por_reconciliacion:
        return ["Posiciones paper: apertura BLOQUEADA porque la reconciliación encontró discrepancias; no se abrió nada."]
    if not datos.entradas_informadas:
        return []
    if not datos.entradas:
        return ["Posiciones paper: ningún candidato para abrir en este ciclo."]
    lineas = ["Posiciones paper:"]
    for e in datos.entradas:
        serie = _ETIQUETA_SERIE.get(e.serie, e.serie.lower())
        if e.resultado == "DESCARTADA":
            detalle = "no se abrió (" + (", ".join(e.motivos) or "sin motivo registrado") + ")"
        else:
            detalle = _ETIQUETA_RESULTADO.get(e.resultado, e.resultado)
        lineas.append(f"• {e.symbol} [{serie}] puntaje {e.puntaje.quantize(_DOS_DECIMALES)}: {detalle}")
    return lineas


def mensaje_resumen_ciclo(datos: DatosResumenCiclo) -> str:
    _exigir_utc(datos.terminado_en)
    lineas = [
        f"RESUMEN DEL CICLO ({_ETIQUETA_ORIGEN[datos.origen]})",
        f"Terminó: {datos.terminado_en:%Y-%m-%d %H:%M} UTC · ciclo {str(datos.ciclo_id)[:8]}",
    ]
    if datos.candidatos_evaluados:
        lineas.append(f"Candidatos evaluados ({len(datos.candidatos_evaluados)}): {', '.join(datos.candidatos_evaluados)}")
    else:
        lineas.append("Candidatos evaluados: ninguno")
    if datos.candidatos_excluidos_por_calidad:
        lineas.append(f"Excluidos por calidad de datos: {', '.join(datos.candidatos_excluidos_por_calidad)}")

    if datos.propuestas:
        lineas.append(f"Propuestas ({len(datos.propuestas)}):")
        for p in datos.propuestas:
            lineas.append(
                f"• {p.symbol}: puntaje {p.puntaje_ponderado.quantize(_DOS_DECIMALES)}, "
                f"{_texto_tier(p.tier_entrada)}, confianza {p.confianza_global}"
            )
        lineas.append("Revisa el detalle y aprueba o rechaza desde la interfaz o `scripts.entries`.")
    else:
        lineas.append(f"Sin propuestas. {_por_que_sin_propuestas(datos)}")

    lineas.extend(_lineas_de_entradas(datos))

    lineas.append(
        f"Costo del ciclo: {_usd(datos.costo_ciclo_usd)} USD · Gasto de IA hoy (UTC): "
        f"{_usd(datos.gasto_hoy_usd)} de {_usd(datos.tope_usd)} USD"
    )
    if datos.saldo_estimado_usd is not None:
        linea = f"Saldo estimado de Anthropic: {_usd(datos.saldo_estimado_usd)} USD (estimación, no la cifra de la consola)"
        if datos.saldo_minimo_por_ciclo_usd is not None and datos.saldo_estimado_usd < datos.saldo_minimo_por_ciclo_usd:
            linea += (
                f"\nNO alcanza para otro ciclo automático (mínimo {_usd(datos.saldo_minimo_por_ciclo_usd)} USD): "
                "recarga y actualiza el saldo declarado en .env."
            )
        lineas.append(linea)
    return "\n".join(lineas)


def mensaje_ciclo_fallido(origen: OrigenCiclo, *, ocurrido_en: datetime, tipo_error: str) -> str:
    """Aviso de que el ciclo NO terminó. Solo lleva el NOMBRE del error, nunca su
    texto: un mensaje de excepción podría traer datos que no deben viajar a un chat."""
    _exigir_utc(ocurrido_en)
    return (
        f"CICLO FALLIDO ({_ETIQUETA_ORIGEN[origen]})\n"
        f"Falló a las {ocurrido_en:%Y-%m-%d %H:%M} UTC con {tipo_error}. "
        "El monitor y los stops siguen funcionando (no usan IA). Revisa los logs (`docker compose logs scheduler`) "
        "y, si quieres reintentar, lánzalo a mano con `scripts\\run_daily_cycle.ps1`."
    )
