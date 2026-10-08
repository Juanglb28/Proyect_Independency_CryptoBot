"""Reglas deterministas de ESCALAMIENTO (Paso 11a-2; sección 9 del plan).

Cuándo el sistema debe AVISAR al usuario. Son funciones puras: reciben datos
ya leídos, devuelven un veredicto o un texto. No leen la base de datos, no
llaman a Binance, no envían nada (eso es de `app/notifications`) y no conocen
ningún LLM (P1). Un aviso NUNCA cambia una orden, una posición ni una regla de
riesgo; la única consecuencia operativa es la de la decisión D5: mientras
Binance figure como caído no se abren entradas nuevas (ver
`binance_figura_caido` y `risk.MotivoRechazoEntrada.BINANCE_CAIDO`).

Definiciones fijadas por el usuario (2026-10-07, Paso 11):

- D2: los avisos de caída y volumen son SOLO de posiciones abiertas. El precio
  de "hace una hora" sale de los snapshots guardados de la posición.
- D3: el z-score del volumen compara la ÚLTIMA vela de 1 h CERRADA contra las
  30 velas de 1 h inmediatamente anteriores.
- D5: "Binance caído" = ningún precio durante más de 5 minutos; el estado se
  guarda en `alertas` (BINANCE_CAIDO / BINANCE_RESTABLECIDO).

Derivado por el asistente (el usuario puede corregirlo; cada punto está
también en `claude/decisiones-paso-11.md`):

- Caída ">5 %" es estricta: exactamente 5 % no avisa.
- La referencia de "hace una hora" es el snapshot más reciente con hora <=
  ahora - 1 h, aceptado solo si no es más viejo que `TOLERANCIA_REFERENCIA`
  respecto de ese instante. Si no hay una referencia así (posición abierta hace
  menos de una hora, o el monitor estuvo apagado) NO se avisa y no se
  estima: dato faltante (P3).
- Desviación estándar MUESTRAL (n-1) de las 30 velas previas; "volumen >3 σ"
  es z > 3 estricto. Con menos de 30 velas previas consecutivas o con
  desviación 0 no hay z-score: no se avisa.
- Un aviso por posición y tipo por hora UTC (caída) o por vela (volumen).
- Orden pendiente: se avisa de TODA entrada que espera aprobación manual, no
  solo de las de más de 200 USD (en v1 todas miden 100 USD; ver
  `app.execution.escalations._ordenes_pendientes`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from app.domain.enums import TipoAlerta
from app.domain.rules.risk import MAX_DRAWDOWN_PCT

# --- Parámetros del plan / decisiones del usuario -----------------------------

CAIDA_1H_UMBRAL = Decimal("0.05")
"""Caída máxima tolerada en 1 h antes de avisar (sección 9 del plan: >5 %)."""

VENTANA_CAIDA = timedelta(hours=1)

Z_VOLUMEN_UMBRAL = Decimal(3)
"""Sección 9 del plan: volumen >3 desviaciones estándar."""

VELAS_REFERENCIA_VOLUMEN = 30
"""Decisión D3: las 30 velas de 1 h previas a la última cerrada."""

DURACION_VELA_1H = timedelta(hours=1)

BINANCE_CAIDO_UMBRAL = timedelta(minutes=5)
"""Sección 9 del plan: Binance caído >5 minutos."""

TOLERANCIA_REFERENCIA = timedelta(minutes=5)
"""Derivado: cuánto más viejo que "hace una hora" puede ser el snapshot de
referencia de la caída. Con un snapshot por chequeo de 60 s el normal es de
menos de 1 minuto; más de 5 minutos significa que hubo un hueco de datos."""

_PCT = Decimal("1E-6")
_UNO = Decimal(1)


def _exigir_utc(instante: datetime) -> None:
    if instante.tzinfo is None or instante.utcoffset() != timedelta(0):
        raise ValueError("Los instantes deben llevar zona horaria UTC explícita (sección 15.3 del plan).")


# --- Caída en 1 hora -----------------------------------------------------------


@dataclass(frozen=True)
class CaidaDetectada:
    precio_referencia: Decimal
    hora_referencia: datetime
    precio_actual: Decimal
    caida_pct: Decimal
    """Porcentaje POSITIVO de caída (6,5 = bajó 6,5 %)."""


def referencia_de_caida_valida(hora_referencia: datetime, *, now: datetime) -> bool:
    """¿El snapshot es una referencia aceptable de "hace una hora"? Debe ser de
    hace una hora o más, pero no más viejo que la tolerancia."""
    _exigir_utc(hora_referencia)
    _exigir_utc(now)
    limite = now - VENTANA_CAIDA
    return limite - TOLERANCIA_REFERENCIA <= hora_referencia <= limite


def evaluar_caida_1h(
    *, precio_referencia: Decimal, hora_referencia: datetime, precio_actual: Decimal, now: datetime
) -> CaidaDetectada | None:
    """Veredicto de caída para UNA posición, o `None` si no hay aviso (sin
    caída suficiente o referencia no válida). Compara sin dividir:
    `precio < referencia × 0,95`."""
    if precio_referencia <= 0 or precio_actual <= 0:
        raise ValueError("Los precios deben ser positivos.")
    if not referencia_de_caida_valida(hora_referencia, now=now):
        return None
    if not precio_actual < precio_referencia * (_UNO - CAIDA_1H_UMBRAL):
        return None
    caida = ((precio_referencia - precio_actual) / precio_referencia * 100).quantize(
        _PCT, rounding=ROUND_HALF_EVEN
    )
    return CaidaDetectada(precio_referencia, hora_referencia, precio_actual, caida)


# --- Volumen atípico ----------------------------------------------------------


def ultima_vela_1h_cerrada(now: datetime) -> datetime:
    """`open_time` de la última vela de 1 h ya CERRADA en `now`."""
    _exigir_utc(now)
    inicio_hora = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return inicio_hora - DURACION_VELA_1H


def horas_de_referencia_volumen(vela_objetivo: datetime) -> list[datetime]:
    """`open_time` esperados de las 30 velas previas a `vela_objetivo`, de la
    más antigua a la más reciente."""
    _exigir_utc(vela_objetivo)
    return [vela_objetivo - DURACION_VELA_1H * k for k in range(VELAS_REFERENCIA_VOLUMEN, 0, -1)]


def calcular_zscore_volumen(volumen_actual: Decimal, volumenes_previos: list[Decimal]) -> Decimal | None:
    """z = (volumen - media) / desviación estándar muestral de los previos.

    `None` si no hay exactamente 30 volúmenes previos o si la desviación es 0
    (el z-score no está definido): en ambos casos hay un dato que falta y no se
    inventa (P3)."""
    n = len(volumenes_previos)
    if n != VELAS_REFERENCIA_VOLUMEN:
        return None
    media = sum(volumenes_previos, Decimal(0)) / n
    varianza = sum(((v - media) ** 2 for v in volumenes_previos), Decimal(0)) / (n - 1)
    if varianza == 0:
        return None
    desviacion = varianza.sqrt()
    return ((volumen_actual - media) / desviacion).quantize(_PCT, rounding=ROUND_HALF_EVEN)


def es_volumen_atipico(zscore: Decimal | None) -> bool:
    return zscore is not None and zscore > Z_VOLUMEN_UMBRAL


# --- Binance caído ------------------------------------------------------------


def binance_figura_caido(ultimo_aviso: TipoAlerta | None) -> bool:
    """Estado actual: es el tipo de la ÚLTIMA alerta del par
    BINANCE_CAIDO / BINANCE_RESTABLECIDO (decisión D5)."""
    return ultimo_aviso == TipoAlerta.BINANCE_CAIDO


def decidir_aviso_binance(
    *,
    ultimo_aviso: TipoAlerta | None,
    posiciones_consultadas: int,
    precios_obtenidos: int,
    ultima_respuesta: datetime | None,
    now: datetime,
) -> TipoAlerta | None:
    """¿Corresponde un aviso de cambio de estado de Binance en este chequeo?

    - `BINANCE_RESTABLECIDO`: figuraba caído y llegó al menos un precio.
    - `BINANCE_CAIDO`: no figuraba caído, se pidió precio al menos una vez, NO
      llegó ninguno y el último precio recibido tiene más de 5 minutos.
    - `None` en los demás casos. En particular, sin posiciones abiertas el
      monitor no pide ningún precio y no puede saber nada: no cambia el
      estado. Sin `ultima_respuesta` (no hay un precio previo registrado)
      tampoco se declara la caída: no se estima desde cuándo falla (P3).
    """
    _exigir_utc(now)
    if posiciones_consultadas <= 0:
        return None
    if precios_obtenidos > 0:
        return TipoAlerta.BINANCE_RESTABLECIDO if binance_figura_caido(ultimo_aviso) else None
    if binance_figura_caido(ultimo_aviso) or ultima_respuesta is None:
        return None
    _exigir_utc(ultima_respuesta)
    if now - ultima_respuesta > BINANCE_CAIDO_UMBRAL:
        return TipoAlerta.BINANCE_CAIDO
    return None


# --- Textos de los avisos (armados por código, nunca por un modelo) -------------

_NO_ACCION = "Es solo un aviso: el sistema no hizo ningún cambio."


def _fmt(valor: Decimal, decimales: int = 2) -> str:
    return f"{valor.quantize(Decimal(1).scaleb(-decimales), rounding=ROUND_HALF_EVEN):f}"


def _hora(instante: datetime) -> str:
    return instante.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def mensaje_caida_1h(symbol: str, caida: CaidaDetectada) -> str:
    return (
        f"CAÍDA {symbol}: -{_fmt(caida.caida_pct)} % en aproximadamente 1 hora "
        f"(de {_fmt(caida.precio_referencia, 4)} a {_fmt(caida.precio_actual, 4)} USDT; "
        f"referencia de las {_hora(caida.hora_referencia)}). Umbral: {_fmt(CAIDA_1H_UMBRAL * 100, 0)} %. "
        f"{_NO_ACCION} El stop de la posición sigue vigilado."
    )


def mensaje_volumen_atipico(symbol: str, *, vela: datetime, zscore: Decimal, volumen: Decimal) -> str:
    return (
        f"VOLUMEN ATÍPICO {symbol}: la vela de 1 h de las {_hora(vela)} tuvo un volumen de "
        f"{_fmt(volumen, 2)}, a {_fmt(zscore)} desviaciones estándar de las 30 velas previas "
        f"(umbral: {_fmt(Z_VOLUMEN_UMBRAL, 0)}). {_NO_ACCION}"
    )


def mensaje_orden_pendiente(symbol: str, *, monto_usd: Decimal, vence: datetime | None) -> str:
    caduca = f" Vence el {_hora(vence)}." if vence is not None else ""
    return (
        f"APROBACIÓN PENDIENTE {symbol}: entrada de {_fmt(monto_usd)} USD que requiere tu aprobación "
        f"manual (pestaña Posiciones).{caduca} Sin tu aprobación no se abre; al vencer se descarta."
    )


def mensaje_binance_caido(*, ultima_respuesta: datetime, now: datetime) -> str:
    minutos = int((now - ultima_respuesta).total_seconds() // 60)
    return (
        f"BINANCE SIN RESPUESTA: no llega ningún precio desde las {_hora(ultima_respuesta)} "
        f"(más de {minutos} min). Mientras siga así no se abrirán entradas nuevas; las posiciones "
        f"abiertas siguen vigiladas y sus stops se aplicarán cuando vuelva a haber precio."
    )


def mensaje_binance_restablecido(*, now: datetime) -> str:
    return f"BINANCE RESTABLECIDO: volvieron a llegar precios ({_hora(now)}). Se reanudan las entradas nuevas."


def mensaje_discrepancia(detalles: list[str]) -> str:
    lineas = "\n".join(f"- {d}" for d in detalles[:5])
    resto = f"\n(y {len(detalles) - 5} más)" if len(detalles) > 5 else ""
    return (
        f"DISCREPANCIA EN LA RECONCILIACIÓN: {len(detalles)} hallazgo(s). El monitor se DETUVO sin "
        f"actuar y no vigila las posiciones hasta que lo revises a mano.\n{lineas}{resto}"
    )


def mensaje_pausa_drawdown(*, equity: Decimal, peak: Decimal, drawdown_pct: Decimal) -> str:
    return (
        f"PAUSA POR DRAWDOWN: el equity ({_fmt(equity)} USD) cayó {_fmt(drawdown_pct)} % desde su pico "
        f"({_fmt(peak)} USD); el límite es -{_fmt(MAX_DRAWDOWN_PCT * 100, 0)} %. No se abrirán entradas nuevas hasta que reanudes a mano. "
        f"Las posiciones siguen vigiladas."
    )
