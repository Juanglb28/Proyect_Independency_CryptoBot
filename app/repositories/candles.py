"""Persistencia de velas (sección 15.7 del plan: idempotencia por
`symbol + timeframe + open_time`).

Esta capa SOLO persiste (sección 15.2: `repositories` no contiene lógica
de negocio). No decide qué símbolo, temporalidad o ventana descargar —
eso vive en `app/data/candle_ingestion.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.data.exchange_client import Candle
from app.db.models.candles import Vela


@dataclass(frozen=True)
class ResultadoUpsert:
    """Qué hizo `upsert_candles`: velas nuevas, velas en curso refrescadas
    (con su `open_time`, para invalidar lo derivado) y velas cerradas
    ignoradas (inmutables)."""

    insertadas: int
    actualizadas: tuple[datetime, ...]
    ignoradas: int


def upsert_candles(session: Session, candles: list[Candle]) -> ResultadoUpsert:
    """Guarda `candles`; una vela EN CURSO se refresca, una CERRADA no se toca.

    `INSERT ... ON CONFLICT (symbol, timeframe, open_time) DO UPDATE ... WHERE
    velas.cerrada = false`: la restricción única de la base decide qué es un
    duplicado (sección 15.7 y P5: un reintento tras un corte nunca crea una vela
    repetida) y la cláusula `WHERE` hace que SOLO una vela guardada como "en
    curso" pueda cambiar de valores; una vela ya cerrada no se modifica ni
    siquiera por código (además lo impide un trigger).

    Antes de este cambio era `DO NOTHING`, y la vela en curso quedaba congelada
    para siempre con los valores parciales del momento de la descarga.

    `Candle["cerrada"]` ausente = cerrada (datos históricos de pruebas); la
    ingesta real siempre la anota.

    No se puede confiar en `result.rowcount` (psycopg3 con RETURNING deja -1),
    así que se pide un RETURNING explícito: `xmax = 0` distingue una fila
    insertada de una actualizada.
    """
    if not candles:
        return ResultadoUpsert(0, (), 0)

    valores = [{**dict(candle), "cerrada": candle.get("cerrada", True)} for candle in candles]
    tabla = Vela.__table__
    insert = pg_insert(tabla).values(valores)
    statement = insert.on_conflict_do_update(
        constraint="uq_velas_symbol_timeframe_open_time",
        set_={
            "open": insert.excluded.open,
            "high": insert.excluded.high,
            "low": insert.excluded.low,
            "close": insert.excluded.close,
            "volume": insert.excluded.volume,
            "cerrada": insert.excluded.cerrada,
        },
        where=tabla.c.cerrada.is_(False),
    ).returning(tabla.c.open_time, literal_column("(xmax = 0)").label("insertada"))
    filas = session.execute(statement).all()
    session.flush()
    insertadas = sum(1 for _, nueva in filas if nueva)
    actualizadas = tuple(sorted(t for t, nueva in filas if not nueva))
    return ResultadoUpsert(insertadas, actualizadas, len(valores) - len(filas))


def save_candles(session: Session, candles: list[Candle]) -> int:
    """Como `upsert_candles`, devolviendo solo cuántas velas eran NUEVAS."""
    return upsert_candles(session, candles).insertadas


def get_candles(
    session: Session,
    symbol: str,
    timeframe: str,
    *,
    solo_cerradas: bool = True,
    tail: int | None = None,
) -> list[Candle]:
    """Lee las velas guardadas de `symbol`/`timeframe`, ordenadas
    ascendentemente por `open_time`. Por defecto SOLO las cerradas: una vela
    en curso tiene valores provisionales y jamás entra a un indicador ni al
    análisis.

    El cálculo de indicadores (`app/data/indicator_calculator.py`) exige
    ese orden: es el mismo orden en que ocurrió el mercado, y ningún
    indicador puede calcularse usando datos que en ese instante todavía no
    existían (sección 15 del plan, look-ahead bias).

    `tail`: si se da, devuelve solo las ÚLTIMAS `tail` velas (igual orden
    ascendente). Lo usa el cálculo incremental de indicadores del ciclo
    horario: solo necesita una cola reciente, no toda la historia. El orden y
    el contenido de esa cola son idénticos al final de la lista completa, así
    que los indicadores de ventana fija (SMA, volumen, distancia 90 d) salen
    EXACTOS, y los recursivos (RSI/ATR de Wilder) salen idénticos a los de
    historia completa a la escala guardada cuando la cola trae suficiente
    calentamiento (ver `app/data/indicator_pipeline.py`). `tail <= 0` se
    comporta como una cola vacía.

    Devuelve `Candle` (el mismo TypedDict de `exchange_client.py`), no el
    modelo ORM `Vela`: así `app/data/indicator_calculator.py`, que consume
    este resultado, no necesita importar SQLAlchemy — igual que
    `candle_ingestion.py` ya mantiene la capa `data` sin depender de la
    capa de persistencia.
    """
    statement = select(Vela).where(Vela.symbol == symbol, Vela.timeframe == timeframe)
    if solo_cerradas:
        statement = statement.where(Vela.cerrada.is_(True))
    if tail is not None:
        # Las últimas `tail` por fecha descendente + límite (evita traer toda
        # la serie para quedarnos con el final), luego se invierte a ascendente.
        if tail <= 0:
            return []
        rows = list(session.execute(statement.order_by(Vela.open_time.desc()).limit(tail)).scalars().all())
        rows.reverse()
    else:
        rows = list(session.execute(statement.order_by(Vela.open_time.asc())).scalars().all())
    return [
        Candle(
            symbol=row.symbol,
            timeframe=row.timeframe,
            open_time=row.open_time,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
            cerrada=row.cerrada,
        )
        for row in rows
    ]


def get_close_at_or_before(
    session: Session, symbol: str, timeframe: str, *, open_time_at_or_before: datetime
) -> tuple[datetime, Decimal] | None:
    """`(open_time, close)` de la última vela con `open_time <= límite`, o
    `None`. Quien llama calcula el límite para que la vela ya esté CERRADA en
    el instante que le interesa (`instante - duración de la vela`)."""
    row = session.execute(
        select(Vela.open_time, Vela.close)
        .where(
            Vela.symbol == symbol,
            Vela.timeframe == timeframe,
            Vela.cerrada.is_(True),
            Vela.open_time <= open_time_at_or_before,
        )
        .order_by(Vela.open_time.desc())
        .limit(1)
    ).one_or_none()
    return None if row is None else (row[0], row[1])


def get_closes_between(
    session: Session, symbol: str, timeframe: str, *, open_time_from: datetime, open_time_to: datetime
) -> list[tuple[datetime, Decimal]]:
    """`(open_time, close)` de las velas con `open_time` en `[desde, hasta]`,
    de la más antigua a la más reciente."""
    rows = session.execute(
        select(Vela.open_time, Vela.close)
        .where(
            Vela.symbol == symbol,
            Vela.timeframe == timeframe,
            Vela.cerrada.is_(True),
            Vela.open_time >= open_time_from,
            Vela.open_time <= open_time_to,
        )
        .order_by(Vela.open_time.asc())
    ).all()
    return [(r[0], r[1]) for r in rows]


def get_latest_open_time(session: Session, symbol: str, timeframe: str) -> datetime | None:
    """`open_time` de la vela CERRADA más reciente, o `None` si no hay."""
    return session.execute(
        select(Vela.open_time)
        .where(Vela.symbol == symbol, Vela.timeframe == timeframe, Vela.cerrada.is_(True))
        .order_by(Vela.open_time.desc())
        .limit(1)
    ).scalar_one_or_none()


def get_volumes_between(
    session: Session, symbol: str, timeframe: str, *, open_time_from: datetime, open_time_to: datetime
) -> dict[datetime, Decimal]:
    """`{open_time: volumen}` de las velas con `open_time` en `[desde, hasta]`.
    Lo usa la regla de volumen atípico; quien llama decide si las velas que
    faltan invalidan el cálculo."""
    rows = session.execute(
        select(Vela.open_time, Vela.volume).where(
            Vela.symbol == symbol,
            Vela.timeframe == timeframe,
            Vela.cerrada.is_(True),
            Vela.open_time >= open_time_from,
            Vela.open_time <= open_time_to,
        )
    ).all()
    return {r[0]: Decimal(r[1]) for r in rows}


def list_velas_sin_cerrar(session: Session, *, symbol: str | None = None) -> list[tuple[str, str, datetime]]:
    """`(symbol, timeframe, open_time)` de las velas guardadas como NO cerradas,
    de la más antigua a la más reciente. Lo usa la reparación del historial y
    el control de la corrida diaria (una vela en curso cuya hora de cierre ya
    pasó es un dato pendiente de corregir)."""
    consulta = select(Vela.symbol, Vela.timeframe, Vela.open_time).where(Vela.cerrada.is_(False))
    if symbol is not None:
        consulta = consulta.where(Vela.symbol == symbol)
    return [(r[0], r[1], r[2]) for r in session.execute(consulta.order_by(Vela.open_time.asc(), Vela.symbol))]
