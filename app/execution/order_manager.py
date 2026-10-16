"""Servicio de entradas del motor de ejecución paper (Paso 8).

Conecta lo que ya existe y está probado por separado:

- las reglas puras (`app.domain.rules.risk` / `execution`) deciden QUÉ se
  abre, a qué monto, con qué cantidad, comisión, stop y objetivo;
- los repositorios (`app.repositories.*`) persisten;
- este módulo solo ordena los pasos y los hace de forma atómica e
  idempotente. **No calcula ninguna regla financiera** y **no importa nada
  de IA**: los agentes nunca están en el camino de una orden (P1/P2 del plan).

Flujo de una entrada:

1. `candidates_from_cycle` convierte las propuestas de un ciclo en
   candidatos (solo tier `NORMAL` / `MAYOR_RIESGO`).
2. `open_entries` libera las aprobaciones vencidas, arma los cupos
   (contando también las entradas pendientes, que reservan cupo), selecciona
   con `select_entries` y para cada entrada elegida:
   - si no requiere aprobación manual, la ejecuta en paper al precio
     observado (`PriceSource`): crea la posición y la orden `FILLED` en la
     MISMA transacción;
   - si la requiere (mayor riesgo), crea una orden `PENDING_APPROVAL` que
     vence al terminar el día UTC del ciclo y reserva cupo.
3. `approve_pending_entry` / `reject_pending_entry` /
   `expire_pending_entries` resuelven las pendientes. Rechazar o expirar
   devuelve todo lo que la entrada reservaba (cupo de posición, tope de
   mayor riesgo y entrada del día), simplemente porque deja de contar.

Garantías (P5):

- **Una sola transacción por operación y ningún `commit` aquí**: lo hace
  quien llama (`session_scope`). Si algo falla a mitad, la posición y la
  orden se revierten juntas: nunca queda una posición sin su orden.
- **Idempotencia**: el `client_order_id` de una entrada es determinista
  (`entry-<proposal_id>`). Reintentar la misma propuesta no crea otra orden
  ni otra posición (`YA_PROCESADA`), aunque esa orden haya sido rechazada o
  haya expirado: una propuesta tiene a lo sumo UNA oportunidad de entrada.
- **Sin precio no hay orden**: si `PriceSource` lanza `PriceUnavailable`, no
  se escribe nada y el resultado es `SIN_PRECIO`.
- Stop y objetivo se calculan sobre el precio de EJECUCIÓN real (decisión del
  2026-10-04), no sobre el precio del análisis. El `decision_snapshot` de la
  propuesta no se toca nunca.

Reconciliación (Paso 11c; sección 9 del plan: "discrepancia en la reconciliación:
bloquear operación hasta resolver"): `open_entries` y `approve_pending_entry`
reconcilian el estado ANTES de hacer nada. Si algo no cuadra no se abre ninguna
entrada (ni se expira nada: se devuelve sin escribir). Rechazar sigue
permitido: solo libera cupos y no abre nada.

Aprobar una entrada mientras el sistema está pausado por drawdown se BLOQUEA
(confirmado por el usuario el 2026-10-05). La pausa significa "no se abren
entradas nuevas"; aprobar es abrir una, aunque su cupo se reservara antes de
la pausa. Rechazarla sigue permitido. Lo mismo vale mientras exista el
archivo `PARAR` (`app.execution.emergency_stop`).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum

from sqlalchemy.orm import Session

from app.domain.enums import EstadoOrden, IntencionOrden, LadoOrden, SerieOperacion, TierEntrada, TipoAlerta
from app.domain.rules.execution import (
    CandidatoEntrada,
    EntradaDescartada,
    select_entries,
)
from app.domain.rules.escalation import binance_figura_caido, mensaje_posicion_abierta
from app.domain.rules.results import serialize_prices
from app.domain.rules.risk import (
    SituacionDeCupos,
    calculate_entry_quantity,
    calculate_fee,
    calculate_initial_levels,
    calculate_position_value,
    sistema_pausado,
)
from app.execution.emergency_stop import EmergencyStop, FileEmergencyStop
from app.execution.price_source import PriceSource, PriceUnavailable
from app.execution.reconciliation import Discrepancia, reconcile
from app.execution.reference_prices import fetch_reference_prices
from app.execution.results_recorder import record_entry
from app.repositories.alertas import AlertaRow, append_alerta, get_ultima_alerta_de_tipos
from app.repositories.analysis import get_propuesta, get_propuestas_de_ciclo
from app.repositories.orders import (
    OrdenRow,
    count_entry_orders,
    create_orden,
    get_orden,
    get_orden_by_client_order_id,
    list_orders,
    list_pending_orders_expired_at,
    resolve_pending_order,
)
from app.repositories.positions import PosicionRow, create_posicion, get_posicion, list_open_posiciones
from app.repositories.system_events import get_ultimo_evento_sistema

logger = logging.getLogger(__name__)

_COMPONENT = "execution.order_manager"
_ESTADOS_QUE_OCUPAN_ENTRADA_DEL_DIA = (EstadoOrden.PENDING_APPROVAL, EstadoOrden.FILLED)


class ResultadoEntrada(str, Enum):
    ABIERTA = "ABIERTA"
    PENDIENTE_APROBACION = "PENDIENTE_APROBACION"
    YA_PROCESADA = "YA_PROCESADA"
    """Esta propuesta ya tuvo su orden de entrada (ejecutada, pendiente,
    rechazada o expirada): no se vuelve a intentar."""
    SIN_PRECIO = "SIN_PRECIO"


class ResultadoAprobacion(str, Enum):
    APROBADA = "APROBADA"
    EXPIRADA = "EXPIRADA"
    NO_PENDIENTE = "NO_PENDIENTE"
    BLOQUEADA_POR_PAUSA = "BLOQUEADA_POR_PAUSA"
    BLOQUEADA_POR_PARADA = "BLOQUEADA_POR_PARADA"
    BLOQUEADA_POR_RECONCILIACION = "BLOQUEADA_POR_RECONCILIACION"
    SIN_PRECIO = "SIN_PRECIO"


@dataclass(frozen=True)
class EntradaProcesada:
    proposal_id: uuid.UUID
    symbol: str
    resultado: ResultadoEntrada
    orden_id: uuid.UUID | None = None
    posicion_id: uuid.UUID | None = None


@dataclass(frozen=True)
class ResultadoAperturaEntradas:
    procesadas: tuple[EntradaProcesada, ...]
    descartadas: tuple[EntradaDescartada, ...]
    expiradas: int
    """Cuántas aprobaciones vencidas se liberaron antes de calcular cupos."""
    discrepancias: tuple[Discrepancia, ...] = ()
    """Si no está vacía, la reconciliación encontró algo que no cuadra y NO se
    procesó ninguna entrada (ni se expiró nada)."""

    @property
    def bloqueada_por_reconciliacion(self) -> bool:
        return bool(self.discrepancias)


def entry_client_order_id(proposal_id: uuid.UUID) -> str:
    """Identificador de idempotencia de la orden de entrada de una propuesta."""
    return f"entry-{proposal_id}"


def _exigir_utc(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("`now` debe ser un datetime con zona horaria UTC explícita (sección 15.3 del plan).")


def _limites_del_dia_utc(now: datetime) -> tuple[datetime, datetime]:
    inicio = datetime.combine(now.astimezone(timezone.utc).date(), time.min, tzinfo=timezone.utc)
    return inicio, inicio + timedelta(days=1)


def build_cupos(
    session: Session, now: datetime, *, emergency_stop: EmergencyStop | None = None
) -> SituacionDeCupos:
    """Foto de cupos para `select_entries`, con las entradas pendientes
    RESERVANDO cupo (decisión del 2026-10-04). Los contadores de posiciones,
    entradas del día y mayor riesgo cuentan SOLO la estrategia; las operaciones
    de observación tienen los suyos (decisión del 2026-10-12) y los símbolos
    abiertos son los de ambas series:

    - posiciones = abiertas + entradas pendientes;
    - entradas de hoy = órdenes `ENTRY` pendientes o ejecutadas creadas en el
      día UTC de `now` (rechazadas y expiradas no cuentan: devuelven el cupo);
    - mayor riesgo = abiertas + pendientes cuya propuesta es `MAYOR_RIESGO`;
    - símbolos = los de las posiciones abiertas y los de las pendientes;
    - parada de emergencia = existe el archivo `PARAR` (ver
      `app.execution.emergency_stop`);
    - Binance caído = el último aviso del par BINANCE_CAIDO/RESTABLECIDO es
      BINANCE_CAIDO (Paso 11a-2, decisión D5): bloquea entradas nuevas.
    """
    _exigir_utc(now)
    abiertas = list_open_posiciones(session, serie=SerieOperacion.ESTRATEGIA)
    abiertas_observacion = list_open_posiciones(session, serie=SerieOperacion.OBSERVACION)
    pendientes = list_orders(
        session, estados=[EstadoOrden.PENDING_APPROVAL], intencion=IntencionOrden.ENTRY
    )

    mayor_riesgo = 0
    for proposal_id in [p.proposal_id for p in abiertas] + [o.proposal_id for o in pendientes]:
        propuesta = get_propuesta(session, proposal_id)
        if propuesta is not None and propuesta.tier_entrada == TierEntrada.MAYOR_RIESGO:
            mayor_riesgo += 1

    desde, hasta = _limites_del_dia_utc(now)
    entradas_hoy = count_entry_orders(
        session,
        estados=_ESTADOS_QUE_OCUPAN_ENTRADA_DEL_DIA,
        created_from=desde,
        created_before=hasta,
        serie=SerieOperacion.ESTRATEGIA,
    )
    observacion_entradas_hoy = count_entry_orders(
        session,
        estados=_ESTADOS_QUE_OCUPAN_ENTRADA_DEL_DIA,
        created_from=desde,
        created_before=hasta,
        serie=SerieOperacion.OBSERVACION,
    )
    ultimo_evento = get_ultimo_evento_sistema(session)
    ultimo_aviso_binance = get_ultima_alerta_de_tipos(
        session, [TipoAlerta.BINANCE_CAIDO, TipoAlerta.BINANCE_RESTABLECIDO]
    )

    return SituacionDeCupos(
        posiciones_abiertas=len(abiertas) + len(pendientes),
        entradas_hoy=entradas_hoy,
        mayor_riesgo_abiertas=mayor_riesgo,
        simbolos_abiertos=frozenset(
            [p.symbol for p in abiertas] + [p.symbol for p in abiertas_observacion] + [o.symbol for o in pendientes]
        ),
        sistema_pausado=sistema_pausado(ultimo_evento.event_type if ultimo_evento else None),
        parada_emergencia=(emergency_stop or FileEmergencyStop()).is_active(),
        binance_caido=binance_figura_caido(ultimo_aviso_binance.tipo if ultimo_aviso_binance else None),
        observacion_abiertas=len(abiertas_observacion),
        observacion_entradas_hoy=observacion_entradas_hoy,
    )


def candidates_from_cycle(session: Session, ciclo_id: uuid.UUID) -> list[CandidatoEntrada]:
    """Propuestas del ciclo que calificaron como `NORMAL` o `MAYOR_RIESGO`. El
    tier y el puntaje ya los calculó el código al guardar la propuesta."""
    return [
        CandidatoEntrada(
            proposal_id=p.id,
            symbol=p.symbol,
            tier=p.tier_entrada,
            puntaje_ponderado=p.puntaje_ponderado,
        )
        for p in get_propuestas_de_ciclo(session, ciclo_id)
        if p.tier_entrada in (TierEntrada.NORMAL, TierEntrada.MAYOR_RIESGO)
    ]


def _valores_de_ejecucion(requested_usd: Decimal, price: Decimal):
    quantity = calculate_entry_quantity(requested_usd, price)
    if quantity <= 0:
        raise ValueError(f"Cantidad no positiva al comprar {requested_usd} USD a {price}")
    fee = calculate_fee(quantity, price)
    niveles = calculate_initial_levels(price)
    return quantity, fee, niveles


def _abrir_posicion(
    session: Session,
    *,
    proposal_id: uuid.UUID,
    symbol: str,
    quantity: Decimal,
    price: Decimal,
    niveles,
    now: datetime,
    price_source: PriceSource,
) -> uuid.UUID:
    # Precios de referencia para el benchmark de la operación (Paso 9): los
    # observados ahora mismo; el del propio símbolo es el de ejecución.
    referencia = fetch_reference_prices(price_source, known={symbol: price})
    posicion_id, creada = create_posicion(
        session,
        PosicionRow(
            proposal_id=proposal_id,
            symbol=symbol,
            entry_price=price,
            initial_quantity=quantity,
            initial_stop_price=niveles.stop_price,
            initial_target_price=niveles.target_price,
            opened_at=now,
            reference_prices_at_entry=serialize_prices(referencia),
        ),
    )
    if not creada:
        # Existe una posición para esta propuesta pero no una orden que la
        # explique: estado incoherente. Se aborta para que la transacción
        # completa se revierta; nunca se "arregla" en silencio.
        raise RuntimeError(f"La propuesta {proposal_id} ya tiene una posición sin orden de entrada asociada")
    return posicion_id


def _avisar_posicion_abierta(
    session: Session,
    *,
    posicion_id: uuid.UUID,
    orden_id: uuid.UUID,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    now: datetime,
    aprobada_por_el_usuario: bool,
) -> None:
    """Deja el aviso de Telegram de una posición recién abierta (decisión del
    2026-10-12). Es solo un aviso (P2): corre en un SAVEPOINT y cualquier fallo se
    registra y se ignora; nunca puede impedir ni revertir la entrada."""
    try:
        with session.begin_nested():
            posicion = get_posicion(session, posicion_id)
            propuesta = get_propuesta(session, posicion.proposal_id)
            if propuesta is not None and propuesta.tier_entrada == TierEntrada.OBSERVACION:
                origen, etiqueta = "observación, sin IA", "Puntaje del pre-filtro"
            else:
                origen, etiqueta = "estrategia", "Puntaje ponderado"
            if aprobada_por_el_usuario:
                origen += ", aprobada por ti"
            append_alerta(
                session,
                AlertaRow(
                    tipo=TipoAlerta.POSICION_ABIERTA,
                    subject=posicion.symbol,
                    dedup_key=f"posicion-abierta:{posicion_id}",
                    occurred_at=now,
                    message=mensaje_posicion_abierta(
                        posicion.symbol,
                        origen=origen,
                        cantidad=quantity,
                        precio=price,
                        valor_usd=calculate_position_value(quantity, price),
                        comision_usd=fee,
                        stop=posicion.initial_stop_price,
                        objetivo=posicion.initial_target_price,
                        puntaje=propuesta.puntaje_ponderado if propuesta is not None else Decimal(0),
                        etiqueta_puntaje=etiqueta,
                    ),
                    observed_value=calculate_position_value(quantity, price),
                    position_id=posicion_id,
                    order_id=orden_id,
                ),
            )
    except Exception:  # noqa: BLE001 - un aviso nunca bloquea una entrada
        logger.error(
            "no se pudo guardar el aviso de posición abierta",
            exc_info=True,
            extra={"component": _COMPONENT, "position_id": str(posicion_id)},
        )


def _ejecutar_entrada_automatica(
    session: Session, *, candidato: CandidatoEntrada, requested_usd: Decimal, now: datetime, price_source: PriceSource
) -> EntradaProcesada:
    client_order_id = entry_client_order_id(candidato.proposal_id)
    if get_orden_by_client_order_id(session, client_order_id) is not None:
        return EntradaProcesada(candidato.proposal_id, candidato.symbol, ResultadoEntrada.YA_PROCESADA)

    try:
        price = price_source.get_price(candidato.symbol)
    except PriceUnavailable:
        return EntradaProcesada(candidato.proposal_id, candidato.symbol, ResultadoEntrada.SIN_PRECIO)

    quantity, fee, niveles = _valores_de_ejecucion(requested_usd, price)
    posicion_id = _abrir_posicion(
        session,
        proposal_id=candidato.proposal_id,
        symbol=candidato.symbol,
        quantity=quantity,
        price=price,
        niveles=niveles,
        now=now,
        price_source=price_source,
    )
    orden_id, creada = create_orden(
        session,
        OrdenRow(
            client_order_id=client_order_id,
            proposal_id=candidato.proposal_id,
            symbol=candidato.symbol,
            side=LadoOrden.BUY,
            intencion=IntencionOrden.ENTRY,
            estado=EstadoOrden.FILLED,
            position_id=posicion_id,
            requested_usd=requested_usd,
            quantity=quantity,
            reference_price=price,
            execution_price=price,
            fee=fee,
            filled_at=now,
            created_at=now,
        ),
    )
    if not creada:  # pragma: no cover - descartado arriba; solo una carrera
        raise RuntimeError(f"client_order_id {client_order_id!r} apareció durante la ejecución")
    record_entry(session, get_posicion(session, posicion_id), now=now)
    _avisar_posicion_abierta(
        session, posicion_id=posicion_id, orden_id=orden_id, quantity=quantity, price=price, fee=fee,
        now=now, aprobada_por_el_usuario=False,
    )
    return EntradaProcesada(
        candidato.proposal_id, candidato.symbol, ResultadoEntrada.ABIERTA, orden_id=orden_id, posicion_id=posicion_id
    )


def _crear_entrada_pendiente(
    session: Session, *, candidato: CandidatoEntrada, requested_usd: Decimal, now: datetime
) -> EntradaProcesada:
    _, vence = _limites_del_dia_utc(now)
    orden_id, creada = create_orden(
        session,
        OrdenRow(
            client_order_id=entry_client_order_id(candidato.proposal_id),
            proposal_id=candidato.proposal_id,
            symbol=candidato.symbol,
            side=LadoOrden.BUY,
            intencion=IntencionOrden.ENTRY,
            estado=EstadoOrden.PENDING_APPROVAL,
            requested_usd=requested_usd,
            expires_at=vence,
            created_at=now,
        ),
    )
    resultado = ResultadoEntrada.PENDIENTE_APROBACION if creada else ResultadoEntrada.YA_PROCESADA
    return EntradaProcesada(candidato.proposal_id, candidato.symbol, resultado, orden_id=orden_id)


def expire_pending_entries(session: Session, now: datetime) -> int:
    """Marca `EXPIRED` las entradas pendientes cuyo vencimiento ya llegó.
    Devuelve cuántas expiró. Al dejar de estar pendientes, sus cupos vuelven."""
    _exigir_utc(now)
    expiradas = 0
    for orden in list_pending_orders_expired_at(session, now):
        if resolve_pending_order(
            session,
            orden.id,
            nuevo_estado=EstadoOrden.EXPIRED,
            resolved_at=now,
            detail="Expiró sin aprobación al terminar el día UTC del ciclo.",
        ):
            expiradas += 1
            _log("entrada expirada", orden_id=orden.id, symbol=orden.symbol)
    return expiradas


def open_entries(
    session: Session,
    candidatos: list[CandidatoEntrada],
    *,
    now: datetime,
    price_source: PriceSource,
    emergency_stop: EmergencyStop | None = None,
) -> ResultadoAperturaEntradas:
    """Procesa los candidatos de un ciclo (ver el docstring del módulo)."""
    _exigir_utc(now)
    discrepancias = reconcile(session)
    if discrepancias:
        _log_bloqueo_por_reconciliacion(discrepancias)
        return ResultadoAperturaEntradas(procesadas=(), descartadas=(), expiradas=0, discrepancias=tuple(discrepancias))
    expiradas = expire_pending_entries(session, now)

    cupos = build_cupos(session, now, emergency_stop=emergency_stop)
    seleccion = select_entries(candidatos, cupos)

    procesadas: list[EntradaProcesada] = []
    for elegida in seleccion.seleccionadas:
        if elegida.requiere_aprobacion_manual:
            procesada = _crear_entrada_pendiente(
                session, candidato=elegida.candidato, requested_usd=elegida.monto_usd, now=now
            )
        else:
            procesada = _ejecutar_entrada_automatica(
                session,
                candidato=elegida.candidato,
                requested_usd=elegida.monto_usd,
                now=now,
                price_source=price_source,
            )
        procesadas.append(procesada)
        _log(
            "entrada procesada",
            proposal_id=procesada.proposal_id,
            symbol=procesada.symbol,
            resultado=procesada.resultado.value,
        )

    for descartada in seleccion.descartadas:
        _log(
            "entrada descartada",
            proposal_id=descartada.candidato.proposal_id,
            symbol=descartada.candidato.symbol,
            motivos=[m.value for m in descartada.motivos],
        )

    return ResultadoAperturaEntradas(
        procesadas=tuple(procesadas), descartadas=seleccion.descartadas, expiradas=expiradas
    )


def approve_pending_entry(
    session: Session,
    orden_id: uuid.UUID,
    *,
    now: datetime,
    price_source: PriceSource,
    emergency_stop: EmergencyStop | None = None,
) -> ResultadoAprobacion:
    """Aprueba una entrada `PENDING_APPROVAL` y la ejecuta en paper al precio
    de este momento. Si venció, la expira en vez de ejecutarla; si el sistema
    está pausado por drawdown o existe el archivo `PARAR`, la bloquea (ver el
    docstring del módulo)."""
    _exigir_utc(now)
    orden = get_orden(session, orden_id)
    if orden is None:
        raise ValueError(f"No existe la orden {orden_id}")
    if orden.intencion != IntencionOrden.ENTRY or orden.estado != EstadoOrden.PENDING_APPROVAL:
        return ResultadoAprobacion.NO_PENDIENTE

    discrepancias = reconcile(session)
    if discrepancias:
        _log_bloqueo_por_reconciliacion(discrepancias, orden_id=orden.id)
        return ResultadoAprobacion.BLOQUEADA_POR_RECONCILIACION

    if orden.expires_at is not None and now >= orden.expires_at:
        resolve_pending_order(
            session,
            orden.id,
            nuevo_estado=EstadoOrden.EXPIRED,
            resolved_at=now,
            detail="Se intentó aprobar después del vencimiento.",
        )
        return ResultadoAprobacion.EXPIRADA

    ultimo_evento = get_ultimo_evento_sistema(session)
    if sistema_pausado(ultimo_evento.event_type if ultimo_evento else None):
        return ResultadoAprobacion.BLOQUEADA_POR_PAUSA
    if (emergency_stop or FileEmergencyStop()).is_active():
        return ResultadoAprobacion.BLOQUEADA_POR_PARADA

    try:
        price = price_source.get_price(orden.symbol)
    except PriceUnavailable:
        return ResultadoAprobacion.SIN_PRECIO

    quantity, fee, niveles = _valores_de_ejecucion(orden.requested_usd, price)
    posicion_id = _abrir_posicion(
        session,
        proposal_id=orden.proposal_id,
        symbol=orden.symbol,
        quantity=quantity,
        price=price,
        niveles=niveles,
        now=now,
        price_source=price_source,
    )
    resuelta = resolve_pending_order(
        session,
        orden.id,
        nuevo_estado=EstadoOrden.FILLED,
        resolved_at=now,
        position_id=posicion_id,
        quantity=quantity,
        execution_price=price,
        fee=fee,
        filled_at=now,
    )
    if not resuelta:
        # Otra resolución se adelantó entre la lectura y el UPDATE: se aborta
        # para revertir también la posición recién creada.
        raise RuntimeError(f"La orden {orden.id} dejó de estar pendiente durante la aprobación")

    record_entry(session, get_posicion(session, posicion_id), now=now)
    _avisar_posicion_abierta(
        session, posicion_id=posicion_id, orden_id=orden.id, quantity=quantity, price=price, fee=fee,
        now=now, aprobada_por_el_usuario=True,
    )
    _log("entrada aprobada", orden_id=orden.id, symbol=orden.symbol, posicion_id=posicion_id)
    return ResultadoAprobacion.APROBADA


def reject_pending_entry(
    session: Session, orden_id: uuid.UUID, *, now: datetime, detail: str | None = None
) -> bool:
    """Rechaza una entrada pendiente. `True` si la rechazó, `False` si ya no
    estaba pendiente. Libera todo lo que reservaba."""
    _exigir_utc(now)
    orden = get_orden(session, orden_id)
    if orden is None:
        raise ValueError(f"No existe la orden {orden_id}")
    if orden.intencion != IntencionOrden.ENTRY:
        return False
    rechazada = resolve_pending_order(
        session,
        orden_id,
        nuevo_estado=EstadoOrden.REJECTED,
        resolved_at=now,
        detail=detail or "Rechazada manualmente.",
    )
    if rechazada:
        _log("entrada rechazada", orden_id=orden_id, symbol=orden.symbol)
    return rechazada


def _log_bloqueo_por_reconciliacion(discrepancias: list[Discrepancia], **context) -> None:
    logger.error(
        "order-manager: entrada bloqueada por discrepancias en la reconciliación",
        extra={
            "component": _COMPONENT,
            "discrepancias": [f"{d.codigo.value}: {d.detalle}" for d in discrepancias],
            **{k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in context.items()},
        },
    )


def _log(message: str, **context) -> None:
    logger.info(
        f"order-manager: {message}",
        extra={"component": _COMPONENT, **{k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in context.items()}},
    )
