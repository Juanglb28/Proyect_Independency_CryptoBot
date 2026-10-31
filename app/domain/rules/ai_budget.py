"""Reglas puras del TOPE DIARIO de gasto de IA (decisión del usuario, 2026-10-10).

El gasto de IA sale de `llamadas_ia` (lo que Anthropic cobra, también las
llamadas fallidas) acumulado desde el inicio del día UTC. Funciones puras: no
leen la base ni llaman a nadie, y la IA nunca decide nada de esto (P1).

Semántica del tope — BLANDO: se compara ANTES de iniciar un ciclo de análisis
automático (el programado y el disparado por candidatos nuevos). Con el gasto de
hoy `>=` el tope no empieza ninguno; un ciclo ya en marcha termina completo,
porque cortarlo a la mitad dejaría un consolidado sobre un subconjunto de
candidatos (datos parciales presentados como completos). El exceso máximo es,
por tanto, el costo de un ciclo. Las corridas MANUALES (`scripts/run_daily_cycle.py`)
no se bloquean: las lanza el usuario a propósito.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal


def presupuesto_diario_agotado(gasto_hoy_usd: Decimal, tope_usd: Decimal) -> bool:
    """`True` si el gasto de hoy ya alcanzó el tope (igual cuenta como agotado)."""
    if tope_usd <= 0:
        raise ValueError("El tope diario de IA debe ser positivo.")
    return gasto_hoy_usd >= tope_usd


def mensaje_presupuesto_ia_agotado(dia: date, *, gasto_hoy_usd: Decimal, tope_usd: Decimal) -> str:
    return (
        f"PRESUPUESTO DE IA AGOTADO: hoy ({dia.isoformat()} UTC) ya se gastaron "
        f"{gasto_hoy_usd.quantize(Decimal('0.01'))} USD de un tope de {tope_usd.quantize(Decimal('0.01'))} USD. "
        f"No se inician más ciclos de análisis con IA hasta las 00:00 UTC. El monitor y los stops siguen "
        f"funcionando (no usan IA). Un ciclo manual (`scripts/run_daily_cycle.py`) no se bloquea."
    )


# ── Guardia de SALDO (decisión del usuario, 2026-10-11) ────────────────────
#
# Con ~0,34 USD de saldo un ciclo (~0,5 USD) se quedaría sin crédito a mitad de
# camino y se cobraría lo ya gastado. La API de Anthropic no expone el saldo real,
# así que se ESTIMA: saldo que el usuario declaró (de la consola) menos lo
# registrado en `llamadas_ia` desde esa fecha. Es una estimación, nunca la cifra
# exacta (ver `Settings.anthropic_saldo_declarado_usd`). Sin saldo declarado no hay
# estimación y la guardia no actúa: no se inventa un número (P3).


def saldo_estimado_usd(saldo_declarado_usd: Decimal, gasto_desde_declaracion_usd: Decimal) -> Decimal:
    return saldo_declarado_usd - gasto_desde_declaracion_usd


def saldo_insuficiente(saldo_estimado: Decimal, minimo_por_ciclo_usd: Decimal) -> bool:
    """`True` si el saldo estimado no alcanza para un ciclo (menor que el mínimo;
    igual alcanza)."""
    if minimo_por_ciclo_usd <= 0:
        raise ValueError("El saldo mínimo por ciclo debe ser positivo.")
    return saldo_estimado < minimo_por_ciclo_usd


def mensaje_saldo_ia_insuficiente(dia: date, *, saldo_estimado: Decimal, minimo_por_ciclo_usd: Decimal) -> str:
    dos = Decimal("0.01")
    return (
        f"SALDO DE IA INSUFICIENTE: el saldo estimado de Anthropic es {saldo_estimado.quantize(dos)} USD, "
        f"menos de los {minimo_por_ciclo_usd.quantize(dos)} USD que se piden para iniciar un ciclo ({dia.isoformat()} UTC). "
        f"No se inician ciclos automáticos con IA. El monitor y los stops siguen funcionando (no usan IA). "
        f"Cuando recargues: pon el saldo real de la consola en ANTHROPIC_SALDO_DECLARADO_USD y la hora en "
        f"ANTHROPIC_SALDO_DECLARADO_EN (.env) y reinicia el scheduler. Es una estimación: puede diferir de la consola."
    )


# ── Verificación previa de la LLAVE (decisión del usuario, 2026-10-30) ─────
#
# El 30/10 la llave de API venció (se había creado con fecha de expiración) y el
# ciclo diario gastó ~17 minutos de backfill e indicadores antes de descubrirlo en la
# primera llamada de IA; el ciclo horario lo reintentó cada hora. Ahora se verifica la
# llave con una llamada mínima ANTES de empezar. El aviso nunca incluye la llave.


def mensaje_llave_ia_rechazada(dia: date, *, detalle: str) -> str:
    return (
        f"LLAVE DE IA RECHAZADA ({dia.isoformat()} UTC): {detalle}. "
        f"No se inician ciclos automáticos con IA hasta que se arregle. El monitor y los stops siguen "
        f"funcionando (no usan IA). Qué hacer: crea una llave nueva en la consola de Anthropic (revisa su "
        f"fecha de expiración), ponla en ANTHROPIC_API_KEY en el .env y recrea el scheduler "
        f"(docker compose up -d --force-recreate scheduler). Un ciclo manual tampoco funcionará hasta entonces."
    )
