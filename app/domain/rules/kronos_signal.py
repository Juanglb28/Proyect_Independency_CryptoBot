"""Reglas puras de la señal experimental de Kronos (enmienda E6, 2026-10-15).

Kronos es un modelo numérico de terceros que pronostica velas. Aquí NO se
ejecuta ningún modelo: solo se convierten trayectorias YA pronosticadas en
cifras (retorno esperado, fracción al alza, dispersión) y se calculan las
métricas con las que se contrasta esa señal contra los resultados realizados.
Todo es determinista, sobre `Decimal`, sin red ni base de datos (P1).

Esta señal es EXPERIMENTAL y está en sombra (P2): nada de lo que se calcula aquí
puede llegar a una orden, a una cantidad, a un stop ni al score. Una prueba de
arquitectura lo exige (`tests/unit/test_architecture.py`).

TODO — DECISIÓN PENDIENTE (se fija tras K0, antes de K1): número de
trayectorias, temperatura, top_p y semilla. Aquí solo hay contenedores de
valores; ninguno de esos números está decidido en este archivo.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal

CORTE_PREENTRENAMIENTO = datetime(2024, 7, 1, tzinfo=timezone.utc)
"""Primer instante de decisión que se considera NO visto por Kronos: el paper
dice que el preentrenamiento llega hasta junio de 2024 y que su prueba empieza en
julio de 2024. Decisión del usuario 2026-10-15 (enmienda E6). La fecha real de los
pesos públicos no está documentada: la sensibilidad con 2025-03-19 se reporta aparte."""

_SEIS = Decimal("1E-6")
_CERO = Decimal(0)


@dataclass(frozen=True)
class ParametrosKronos:
    """Todo lo que, junto con los datos, determina un pronóstico. Se guarda
    con cada pronóstico para poder reproducirlo (trazabilidad)."""

    model_id: str  # p. ej. "NeoQuasar/Kronos-small"
    tokenizer_id: str  # p. ej. "NeoQuasar/Kronos-Tokenizer-base"
    weights_revision: str  # hash de la revisión de Hugging Face del modelo (no "main")
    tokenizer_revision: str  # ídem para el tokenizador
    code_commit: str  # commit del repositorio de Kronos
    lookback: int  # velas de 1 h de contexto
    horizon: int  # velas de 1 h a pronosticar
    n_trajectories: int
    temperature: Decimal
    top_p: Decimal
    seed: int
    usa_volumen: bool = False  # el preentrenamiento de cripto fue solo OHLC


def semilla_de_contexto(semilla_base: int, symbol: str, as_of: datetime) -> int:
    """Semilla propia de cada pronóstico: depende SOLO de la semilla base, el símbolo
    y el instante de decisión. Así el pronóstico de (símbolo, as_of) no cambia según
    qué otros contextos se procesen a la vez ni en qué orden (el muestreo de Kronos
    usa el generador global de torch). Se usa SHA-256 y no `hash()` porque este
    último varía entre procesos."""
    if as_of.tzinfo is None:
        raise ValueError("as_of debe tener zona horaria.")
    crudo = f"{semilla_base}|{symbol}|{as_of.astimezone(timezone.utc).isoformat()}".encode()
    return int.from_bytes(hashlib.sha256(crudo).digest()[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


def validar_parametros(p: ParametrosKronos, *, max_context: int = 512) -> None:
    """Rechaza combinaciones imposibles. `lookback + horizon` debe caber en el
    contexto del modelo (512 en small/base): si no, el modelo truncaría el
    contexto en silencio."""
    if p.lookback <= 0 or p.horizon <= 0 or p.n_trajectories <= 0:
        raise ValueError("lookback, horizon y n_trajectories deben ser positivos.")
    if p.lookback + p.horizon > max_context:
        raise ValueError(
            f"lookback + horizon ({p.lookback + p.horizon}) excede el contexto máximo del modelo ({max_context})."
        )
    if p.temperature <= 0:
        raise ValueError("La temperatura debe ser positiva.")
    if not (_CERO < p.top_p <= 1):
        raise ValueError("top_p debe estar en (0, 1].")
    for nombre, valor in (("weights_revision", p.weights_revision), ("tokenizer_revision", p.tokenizer_revision)):
        if not valor or valor.lower() in {"main", "master", "latest"}:
            raise ValueError(f"{nombre} debe ser un hash fijo, no una rama móvil.")


@dataclass(frozen=True)
class ResumenKronos:
    """Resumen numérico de N trayectorias pronosticadas para un símbolo/instante."""

    n: int
    expected_return: Decimal  # media de (cierre_final / ultimo_cierre - 1)
    prob_up: Decimal  # fracción de trayectorias con retorno > 0
    dispersion: Decimal | None  # desviación estándar muestral (None con n < 2)


def resumir_trayectorias(ultimo_cierre: Decimal, cierres_finales: Sequence[Decimal]) -> ResumenKronos:
    """Convierte los cierres finales de cada trayectoria en el resumen de la señal.

    `ultimo_cierre`: cierre de la última vela CONOCIDA en el instante de la
    decisión. Falla (no inventa) si no hay trayectorias o si algún precio no es
    positivo."""
    if ultimo_cierre <= 0:
        raise ValueError("El último cierre debe ser positivo.")
    if not cierres_finales:
        raise ValueError("No hay trayectorias que resumir (dato faltante, no se estima).")
    if any(c <= 0 for c in cierres_finales):
        raise ValueError("Una trayectoria produjo un cierre no positivo; el pronóstico no es utilizable.")
    n = len(cierres_finales)
    retornos = [c / ultimo_cierre - 1 for c in cierres_finales]
    media = sum(retornos, _CERO) / n
    al_alza = sum(1 for r in retornos if r > 0)
    if n >= 2:
        varianza = sum(((r - media) ** 2 for r in retornos), _CERO) / (n - 1)
        dispersion: Decimal | None = varianza.sqrt().quantize(_SEIS, rounding=ROUND_HALF_EVEN)
    else:
        dispersion = None
    return ResumenKronos(
        n=n,
        expected_return=media.quantize(_SEIS, rounding=ROUND_HALF_EVEN),
        prob_up=(Decimal(al_alza) / n).quantize(_SEIS, rounding=ROUND_HALF_EVEN),
        dispersion=dispersion,
    )


# --- Métricas de contraste (K0/K1): señal vs. retorno REALIZADO -----------------


@dataclass(frozen=True)
class PuntoContraste:
    """Un pronóstico y lo que luego ocurrió. `retorno_realizado` solo se conoce
    DESPUÉS del instante de decisión y jamás entra al pronóstico."""

    symbol: str
    dia: str  # fecha ISO del instante de decisión (agrupa el ranking diario)
    retorno_esperado: Decimal
    retorno_realizado: Decimal


@dataclass(frozen=True)
class SanidadVsPersistencia:
    """Chequeo de cordura de K0 (NO es evidencia de capacidad predictiva)."""

    n: int
    mae_kronos: Decimal  # |esperado - realizado| medio
    mae_persistencia: Decimal  # |0 - realizado| medio: predecir "no cambia"
    kronos_mejor_que_persistencia: bool
    acierto_direccion: Decimal | None  # fracción con signo igual (sin contar realizados == 0)
    tasa_base_alza: Decimal | None  # fracción de realizados > 0 (referencia del acierto)


def sanidad_vs_persistencia(puntos: Sequence[PuntoContraste]) -> SanidadVsPersistencia:
    if not puntos:
        raise ValueError("No hay puntos para contrastar.")
    n = len(puntos)
    mae_k = sum((abs(p.retorno_esperado - p.retorno_realizado) for p in puntos), _CERO) / n
    mae_p = sum((abs(p.retorno_realizado) for p in puntos), _CERO) / n
    con_signo = [p for p in puntos if p.retorno_realizado != 0]
    if con_signo:
        aciertos = sum(1 for p in con_signo if (p.retorno_esperado > 0) == (p.retorno_realizado > 0))
        acierto: Decimal | None = (Decimal(aciertos) / len(con_signo)).quantize(_SEIS)
        base: Decimal | None = (
            Decimal(sum(1 for p in con_signo if p.retorno_realizado > 0)) / len(con_signo)
        ).quantize(_SEIS)
    else:
        acierto = base = None
    return SanidadVsPersistencia(
        n=n,
        mae_kronos=mae_k.quantize(_SEIS, rounding=ROUND_HALF_EVEN),
        mae_persistencia=mae_p.quantize(_SEIS, rounding=ROUND_HALF_EVEN),
        kronos_mejor_que_persistencia=mae_k < mae_p,
        acierto_direccion=acierto,
        tasa_base_alza=base,
    )


def _rangos(valores: Sequence[Decimal]) -> list[Decimal]:
    """Rangos 1..n con promedio en empates (rango medio), para Spearman."""
    orden = sorted(range(len(valores)), key=lambda i: valores[i])
    rangos = [_CERO] * len(valores)
    i = 0
    while i < len(orden):
        j = i
        while j + 1 < len(orden) and valores[orden[j + 1]] == valores[orden[i]]:
            j += 1
        medio = Decimal(i + j) / 2 + 1
        for k in range(i, j + 1):
            rangos[orden[k]] = medio
        i = j + 1
    return rangos


def spearman(xs: Sequence[Decimal], ys: Sequence[Decimal]) -> Decimal | None:
    """Correlación de rangos de Spearman. `None` si hay menos de 3 pares o si una
    de las series no varía (correlación no definida: no se rellena con 0)."""
    if len(xs) != len(ys):
        raise ValueError("Las series deben tener la misma longitud.")
    n = len(xs)
    if n < 3:
        return None
    rx, ry = _rangos(xs), _rangos(ys)
    mx, my = sum(rx, _CERO) / n, sum(ry, _CERO) / n
    cov = sum(((a - mx) * (b - my) for a, b in zip(rx, ry)), _CERO)
    vx = sum(((a - mx) ** 2 for a in rx), _CERO)
    vy = sum(((b - my) ** 2 for b in ry), _CERO)
    if vx == 0 or vy == 0:
        return None
    return (cov / (vx * vy).sqrt()).quantize(_SEIS, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class RankICDiario:
    dias_usados: int
    dias_excluidos: int  # días con menos de `minimo_por_dia` símbolos o correlación no definida
    promedio: Decimal | None


def rank_ic_diario(puntos: Sequence[PuntoContraste], *, minimo_por_dia: int = 10) -> RankICDiario:
    """Promedio de la correlación de Spearman, DÍA POR DÍA, entre el retorno
    esperado y el realizado (ranking entre monedas dentro del mismo día: el uso
    real). `minimo_por_dia` es el mismo parámetro de análisis declarado en el
    walk-forward (≥ 10 monedas por día)."""
    por_dia: dict[str, list[PuntoContraste]] = {}
    for p in puntos:
        por_dia.setdefault(p.dia, []).append(p)
    valores: list[Decimal] = []
    excluidos = 0
    for dia in sorted(por_dia):
        grupo = por_dia[dia]
        rho = (
            spearman([p.retorno_esperado for p in grupo], [p.retorno_realizado for p in grupo])
            if len(grupo) >= minimo_por_dia
            else None
        )
        if rho is None:
            excluidos += 1
        else:
            valores.append(rho)
    promedio = (sum(valores, _CERO) / len(valores)).quantize(_SEIS, rounding=ROUND_HALF_EVEN) if valores else None
    return RankICDiario(dias_usados=len(valores), dias_excluidos=excluidos, promedio=promedio)
