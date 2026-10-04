"""Reglas de riesgo del motor de ejecución paper (sección 9 del plan y
"Decisiones del Paso 8" del README, cerradas por el usuario el 2026-10-04).

Todo aquí es PURO y determinista: recibe números, devuelve decisiones. No
lee la base de datos, no llama a Binance, no conoce a ningún LLM ni a
Streamlit (principios P1/P2). Quien persiste, reintenta o pide aprobación
son los servicios de `app/execution/`; este módulo solo responde "qué dice
la regla". Por eso se puede probar exhaustivamente en los umbrales exactos,
que es lo único que el plan declara no negociable ("las reglas de riesgo
tienen cobertura de pruebas al 100%").

Las constantes viven en código versionado, NUNCA en `.env` (sección 10 del
plan: los valores de seguridad no pueden cambiarse desde la configuración).

Convenciones:

- Dinero y precios en `Decimal`, jamás `float`. Los resultados se redondean
  a la escala de su columna en la base de datos (`NUMERIC(30,12)` para
  precios y cantidades, `NUMERIC(20,8)` para USD) con la regla explícita de
  cada función, de modo que lo calculado aquí es EXACTAMENTE lo que queda
  guardado y los umbrales no cambian al ir y volver de la base.
- Un nivel se considera alcanzado en la igualdad exacta: el stop se activa
  con `precio <= stop`, el objetivo con `precio >= objetivo`, el trailing
  con `precio <= máximo × (1 - 6%)` y la pausa con `equity <= pico × (1 -
  15%)`. "Stop exacto en -8%" significa que ESE precio ya dispara.
- El precio que se compara es el precio OBSERVADO en el chequeo (decisión
  del 2026-10-04): si salta de -7% a -9% entre dos chequeos de 60 s, la
  salida se simula al precio observado, no al nivel. Esa decisión la
  ejecuta el servicio que crea la orden; aquí solo se decide SI se sale.
- Ante un estado incoherente (p. ej. TP ejecutado pero trailing apagado)
  se lanza `ValueError`, nunca se adivina qué quiso decir (P3 / sección 30
  del master prompt).

`TODO — DECISIÓN PENDIENTE`: la base del tope "25% del capital" (¿capital
inicial o equity actual?) no está definida. Con el tamaño fijo de 100 USD
decidido el 2026-10-04 es irrelevante (100 < 250 en cualquier equity
razonable), así que aquí solo se valida el tope absoluto de 250 USD.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from enum import Enum

from app.domain.enums import IntencionOrden, TierEntrada, TipoEventoSistema
from app.domain.symbols import ALLOWED_SYMBOLS

# --- Reglas fijas de la sección 9 del plan -------------------------------

STOP_LOSS_PCT = Decimal("0.08")
"""Stop loss obligatorio: -8% desde la entrada."""

TAKE_PROFIT_PCT = Decimal("0.12")
"""Primer objetivo: +12% desde la entrada."""

TAKE_PROFIT_FRACCION_A_CERRAR = Decimal("0.5")
"""Al tocar el objetivo se cierra el 50% de la posición."""

TRAILING_PCT = Decimal("0.06")
"""Trailing del 6% sobre el resto, medido desde el precio máximo observado
desde que se activó (derivado del plan, ver README)."""

MAX_DRAWDOWN_PCT = Decimal("0.15")
"""Pausa total de entradas con drawdown de -15% desde el pico de equity."""

MAX_POSICIONES_ABIERTAS = 4
MAX_ENTRADAS_POR_DIA = 3
MAX_MAYOR_RIESGO_ABIERTAS = 1
"""Tope propio de las entradas de mayor riesgo (decisión del 2026-10-04):
además de compartir los cupos generales."""

TAMANO_POSICION_USD = Decimal(100)
"""Tamaño FIJO de cada entrada en v1 (decisión del 2026-10-04): el mínimo del
plan. Mantiene el tamaño fuera de la validación del score."""
TAMANO_MINIMO_USD = Decimal(100)
TAMANO_MAXIMO_USD = Decimal(250)
UMBRAL_APROBACION_MANUAL_USD = Decimal(200)
"""Una orden sobre este monto (estrictamente mayor) exige aprobación manual."""

COMISION_POR_LADO = Decimal("0.001")
"""0,1% por lado (plan, sección 1). El slippage de paper no se modela: queda
NULL y se trata como 0 (decisión del 2026-10-04)."""

_ESCALA_PRECIO = Decimal("1E-12")  # NUMERIC(30,12)
_ESCALA_USD = Decimal("1E-8")  # NUMERIC(20,8)
_UNO = Decimal(1)


def _precio(valor: Decimal) -> Decimal:
    return valor.quantize(_ESCALA_PRECIO, rounding=ROUND_HALF_EVEN)


def _usd(valor: Decimal) -> Decimal:
    return valor.quantize(_ESCALA_USD, rounding=ROUND_HALF_EVEN)


def _exigir_positivo(nombre: str, valor: Decimal) -> None:
    if valor <= 0:
        raise ValueError(f"{nombre} debe ser mayor que 0, llegó {valor}")


# --- Niveles iniciales, cantidades y comisiones --------------------------


@dataclass(frozen=True)
class NivelesIniciales:
    stop_price: Decimal
    target_price: Decimal


def calculate_initial_levels(entry_price: Decimal) -> NivelesIniciales:
    """Stop (-8%) y primer objetivo (+12%) desde el precio de entrada. Quedan
    congelados en `posiciones.initial_stop_price`/`initial_target_price` y en
    el `decision_snapshot`; el trailing nunca los reescribe."""
    _exigir_positivo("entry_price", entry_price)
    return NivelesIniciales(
        stop_price=_precio(entry_price * (_UNO - STOP_LOSS_PCT)),
        target_price=_precio(entry_price * (_UNO + TAKE_PROFIT_PCT)),
    )


def calculate_entry_quantity(requested_usd: Decimal, execution_price: Decimal) -> Decimal:
    """Cantidad comprada con `requested_usd` al precio de ejecución, redondeada
    HACIA ABAJO a 12 decimales: nunca se compra más de lo que se pidió. La
    comisión NO se descuenta de la cantidad: se cobra aparte (sección 8.4 del
    plan: `position_value_at_entry = entry_quantity × entry_price`, con
    `entry_fee` separada)."""
    _exigir_positivo("requested_usd", requested_usd)
    _exigir_positivo("execution_price", execution_price)
    return (requested_usd / execution_price).quantize(_ESCALA_PRECIO, rounding=ROUND_DOWN)


def calculate_fee(quantity: Decimal, execution_price: Decimal) -> Decimal:
    """Comisión en USD de una ejecución: 0,1% de `cantidad × precio`,
    redondeada a 8 decimales (`NUMERIC(20,8)`)."""
    _exigir_positivo("quantity", quantity)
    _exigir_positivo("execution_price", execution_price)
    return _usd(quantity * execution_price * COMISION_POR_LADO)


# --- Monitoreo de una posición abierta -----------------------------------


@dataclass(frozen=True)
class EstadoParaMonitor:
    """Lo mínimo de `posiciones` que necesita la regla de monitoreo."""

    remaining_quantity: Decimal
    initial_stop_price: Decimal
    initial_target_price: Decimal
    take_profit_hit: bool
    trailing_active: bool
    trailing_peak_price: Decimal | None


class AccionMonitor(str, Enum):
    MANTENER = "MANTENER"
    CERRAR_PARCIAL = "CERRAR_PARCIAL"
    CERRAR_TOTAL = "CERRAR_TOTAL"


@dataclass(frozen=True)
class DecisionMonitor:
    """Resultado de evaluar una posición ante un precio observado. Incluye
    el estado de trailing que el servicio debe persistir, aunque la acción
    sea `MANTENER` (el máximo sube aunque no haya orden)."""

    accion: AccionMonitor
    intencion: IntencionOrden | None
    cantidad_a_vender: Decimal
    take_profit_hit: bool
    trailing_active: bool
    trailing_peak_price: Decimal | None
    trailing_stop_price: Decimal | None
    """Nivel de salida del trailing vigente tras esta evaluación (para el
    snapshot de la trayectoria); `None` si el trailing no está activo."""


def evaluate_position(estado: EstadoParaMonitor, observed_price: Decimal) -> DecisionMonitor:
    """Reglas de la tabla "Acciones automáticas" (sección 9 del plan):

    - sin TP ejecutado: `precio <= stop` -> cierre total (`STOP_LOSS`);
      `precio >= objetivo` -> cierre del 50% (`TAKE_PROFIT_PARTIAL`) y el
      trailing se activa con el precio observado como primer máximo;
    - con trailing activo: el máximo solo sube; `precio <= máximo × 94%` ->
      cierre del resto (`TRAILING_STOP`).

    Una vez ejecutado el TP, el nivel del trailing (>= objetivo × 94%, o sea
    >= +5,28% sobre la entrada) queda siempre por encima del stop inicial
    (-8%), así que el stop original queda superado sin lógica aparte.
    """
    _exigir_positivo("observed_price", observed_price)
    _exigir_positivo("remaining_quantity", estado.remaining_quantity)

    if estado.take_profit_hit != estado.trailing_active:
        raise ValueError(
            "estado incoherente: el TP parcial ejecutado y el trailing activo deben ir juntos "
            f"(take_profit_hit={estado.take_profit_hit}, trailing_active={estado.trailing_active})"
        )

    if estado.trailing_active:
        if estado.trailing_peak_price is None:
            raise ValueError("estado incoherente: trailing activo sin trailing_peak_price")
        peak = max(estado.trailing_peak_price, observed_price)
        trailing_stop = _precio(peak * (_UNO - TRAILING_PCT))
        if observed_price <= trailing_stop:
            return DecisionMonitor(
                accion=AccionMonitor.CERRAR_TOTAL,
                intencion=IntencionOrden.TRAILING_STOP,
                cantidad_a_vender=estado.remaining_quantity,
                take_profit_hit=True,
                trailing_active=True,
                trailing_peak_price=peak,
                trailing_stop_price=trailing_stop,
            )
        return DecisionMonitor(
            accion=AccionMonitor.MANTENER,
            intencion=None,
            cantidad_a_vender=Decimal(0),
            take_profit_hit=True,
            trailing_active=True,
            trailing_peak_price=peak,
            trailing_stop_price=trailing_stop,
        )

    if observed_price <= estado.initial_stop_price:
        return DecisionMonitor(
            accion=AccionMonitor.CERRAR_TOTAL,
            intencion=IntencionOrden.STOP_LOSS,
            cantidad_a_vender=estado.remaining_quantity,
            take_profit_hit=False,
            trailing_active=False,
            trailing_peak_price=None,
            trailing_stop_price=None,
        )

    if observed_price >= estado.initial_target_price:
        a_vender = (estado.remaining_quantity * TAKE_PROFIT_FRACCION_A_CERRAR).quantize(
            _ESCALA_PRECIO, rounding=ROUND_DOWN
        )
        if a_vender <= 0 or a_vender >= estado.remaining_quantity:
            raise ValueError(
                f"la cantidad restante {estado.remaining_quantity} es demasiado pequeña para un cierre parcial"
            )
        return DecisionMonitor(
            accion=AccionMonitor.CERRAR_PARCIAL,
            intencion=IntencionOrden.TAKE_PROFIT_PARTIAL,
            cantidad_a_vender=a_vender,
            take_profit_hit=True,
            trailing_active=True,
            trailing_peak_price=observed_price,
            trailing_stop_price=_precio(observed_price * (_UNO - TRAILING_PCT)),
        )

    return DecisionMonitor(
        accion=AccionMonitor.MANTENER,
        intencion=None,
        cantidad_a_vender=Decimal(0),
        take_profit_hit=False,
        trailing_active=False,
        trailing_peak_price=None,
        trailing_stop_price=None,
    )


# --- Drawdown del sistema ------------------------------------------------


def update_peak_equity(peak_equity: Decimal, equity: Decimal) -> Decimal:
    """El pico de equity solo sube (nunca se reduce)."""
    _exigir_positivo("peak_equity", peak_equity)
    if equity < 0:
        raise ValueError(f"equity no puede ser negativa, llegó {equity}")
    return max(peak_equity, equity)


def calculate_drawdown_pct(equity: Decimal, peak_equity: Decimal) -> Decimal:
    """Drawdown en porcentaje (<= 0), contra el pico de equity (decisión del
    2026-10-04, la misma definición de Maximum Drawdown de la sección 8.6 del
    plan). Solo para reportar: la decisión de pausar usa
    `is_drawdown_limit_reached`, que compara sin dividir."""
    peak = update_peak_equity(peak_equity, equity)
    return ((equity - peak) / peak * 100).quantize(Decimal("1E-6"), rounding=ROUND_HALF_EVEN)


def is_drawdown_limit_reached(equity: Decimal, peak_equity: Decimal) -> bool:
    """True si el drawdown llegó a -15% (igualdad incluida). Compara
    `equity <= pico × 85%` en vez de dividir, para no depender del redondeo
    de una división: 850 sobre un pico de 1.000 pausa exactamente."""
    peak = update_peak_equity(peak_equity, equity)
    return equity <= peak * (_UNO - MAX_DRAWDOWN_PCT)


def sistema_pausado(ultimo_evento: TipoEventoSistema | None) -> bool:
    """¿Está el sistema pausado por drawdown? Depende solo del tipo del
    ÚLTIMO evento de `eventos_sistema` (decisión del 2026-10-04): sin eventos
    o último `REANUDACION` -> no pausado; último `PAUSA_DRAWDOWN` -> pausado.
    Una pausa solo bloquea entradas nuevas; las posiciones abiertas siguen
    vigiladas."""
    return ultimo_evento == TipoEventoSistema.PAUSA_DRAWDOWN


# --- Validación de una entrada nueva -------------------------------------


class MotivoRechazoEntrada(str, Enum):
    SISTEMA_PAUSADO = "SISTEMA_PAUSADO"
    SIMBOLO_NO_PERMITIDO = "SIMBOLO_NO_PERMITIDO"
    TIER_NO_CALIFICA = "TIER_NO_CALIFICA"
    SIMBOLO_YA_ABIERTO = "SIMBOLO_YA_ABIERTO"
    MAXIMO_POSICIONES = "MAXIMO_POSICIONES"
    MAXIMO_ENTRADAS_DIA = "MAXIMO_ENTRADAS_DIA"
    MAXIMO_MAYOR_RIESGO = "MAXIMO_MAYOR_RIESGO"
    TAMANO_FUERA_DE_RANGO = "TAMANO_FUERA_DE_RANGO"


@dataclass(frozen=True)
class SituacionDeCupos:
    """Foto de los cupos al momento de validar. La calcula el servicio
    consultando `posiciones`/`ordenes` (no hay contadores aparte).

    Decisión del usuario (2026-10-04): una orden de entrada
    `PENDING_APPROVAL` RESERVA cupo hasta que se apruebe, se rechace o expire
    (a las 00:00 UTC). Por eso quien arme esta foto debe contar:

    - `posiciones_abiertas`: posiciones `OPEN` + entradas pendientes (que
      todavía no tienen posición, así que no se cuentan dos veces);
    - `entradas_hoy`: órdenes `ENTRY` ejecutadas o pendientes creadas en el
      día UTC en curso;
    - `mayor_riesgo_abiertas`: posiciones `OPEN` de mayor riesgo + entradas
      pendientes de mayor riesgo;
    - `simbolos_abiertos`: símbolos con posición `OPEN` o entrada pendiente.

    Este módulo no consulta nada: cuenta lo que el servicio le entregue."""

    posiciones_abiertas: int
    entradas_hoy: int
    mayor_riesgo_abiertas: int
    simbolos_abiertos: frozenset[str]
    sistema_pausado: bool


@dataclass(frozen=True)
class ValidacionEntrada:
    permitida: bool
    requiere_aprobacion_manual: bool
    monto_usd: Decimal
    motivos_rechazo: tuple[MotivoRechazoEntrada, ...]


def validate_entry(
    *, symbol: str, tier: TierEntrada, cupos: SituacionDeCupos, monto_usd: Decimal = TAMANO_POSICION_USD
) -> ValidacionEntrada:
    """Valida una entrada contra TODAS las reglas y devuelve todos los
    motivos de rechazo, no solo el primero (trazabilidad: sección 14 del
    master prompt).

    La aprobación manual se exige siempre para `MAYOR_RIESGO` y para
    cualquier monto sobre 200 USD. Esta función no recalcula el puntaje ni
    el tier: recibe el `tier` ya clasificado en código por
    `app.domain.rules.entry_tier.clasificar_tier` (P2: la IA nunca llega
    aquí)."""
    motivos: list[MotivoRechazoEntrada] = []

    if cupos.sistema_pausado:
        motivos.append(MotivoRechazoEntrada.SISTEMA_PAUSADO)
    if symbol not in ALLOWED_SYMBOLS:
        motivos.append(MotivoRechazoEntrada.SIMBOLO_NO_PERMITIDO)
    if tier == TierEntrada.NINGUNO:
        motivos.append(MotivoRechazoEntrada.TIER_NO_CALIFICA)
    if symbol in cupos.simbolos_abiertos:
        motivos.append(MotivoRechazoEntrada.SIMBOLO_YA_ABIERTO)
    if cupos.posiciones_abiertas >= MAX_POSICIONES_ABIERTAS:
        motivos.append(MotivoRechazoEntrada.MAXIMO_POSICIONES)
    if cupos.entradas_hoy >= MAX_ENTRADAS_POR_DIA:
        motivos.append(MotivoRechazoEntrada.MAXIMO_ENTRADAS_DIA)
    if tier == TierEntrada.MAYOR_RIESGO and cupos.mayor_riesgo_abiertas >= MAX_MAYOR_RIESGO_ABIERTAS:
        motivos.append(MotivoRechazoEntrada.MAXIMO_MAYOR_RIESGO)
    if not (TAMANO_MINIMO_USD <= monto_usd <= TAMANO_MAXIMO_USD):
        motivos.append(MotivoRechazoEntrada.TAMANO_FUERA_DE_RANGO)

    requiere_aprobacion = tier == TierEntrada.MAYOR_RIESGO or monto_usd > UMBRAL_APROBACION_MANUAL_USD
    return ValidacionEntrada(
        permitida=not motivos,
        requiere_aprobacion_manual=requiere_aprobacion,
        monto_usd=monto_usd,
        motivos_rechazo=tuple(motivos),
    )
