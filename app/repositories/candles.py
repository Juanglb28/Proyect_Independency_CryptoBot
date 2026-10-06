"""Persistencia de velas (sección 15.7 del plan: idempotencia por
`symbol + timeframe + open_time`).

Esta capa SOLO persiste (sección 15.2: `repositories` no contiene lógica
de negocio). No decide qué símbolo, temporalidad o ventana descargar —
eso vive en `app/data/candle_ingestion.py`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.data.exchange_client import Candle
from app.db.models.candles import Vela


def save_candles(session: Session, candles: list[Candle]) -> int:
    """Inserta `candles`, ignorando las que ya existan.

    Usa `INSERT ... ON CONFLICT (symbol, timeframe, open_time) DO NOTHING`
    en vez de "buscar primero, insertar si no existe": esto último tiene
    una condición de carrera entre el SELECT y el INSERT, y además exige
    dos viajes a la base de datos por vela. El `ON CONFLICT` deja que sea
    la restricción única de la base de datos —no el código de la
    aplicación— la que decide qué es un duplicado, que es exactamente lo
    que permite que un reintento tras un corte a mitad de una ingesta
    (sección 15.7 y P5 del plan) nunca cree una vela repetida.

    Devuelve cuántas filas se insertaron realmente (las que ya existían no
    se cuentan), para que quien orquesta el backfill pueda distinguir
    "no había nada nuevo" de "se guardó algo".
    """
    if not candles:
        return 0

    # No se puede confiar en `result.rowcount` aquí: SQLAlchemy agrega un
    # RETURNING implícito para leer el `id` autogenerado de cada fila
    # insertada, y con RETURNING presente psycopg3 deja de reportar
    # `rowcount` de forma confiable (queda fijo en -1). Esto se detectó con
    # los tests de integración de este módulo, que fallaban con conteos
    # negativos.
    #
    # La solución es pedir un RETURNING explícito (basta una columna) y
    # contar las filas devueltas: con `ON CONFLICT DO NOTHING`, Postgres
    # solo emite una fila de RETURNING por cada inserción que sí ocurrió —
    # las que chocaron con el conflicto no aparecen. Contar `len(...)` es
    # entonces exactamente "cuántas velas eran nuevas".
    statement = (
        pg_insert(Vela.__table__)
        .values([dict(candle) for candle in candles])
        .on_conflict_do_nothing(constraint="uq_velas_symbol_timeframe_open_time")
        .returning(Vela.__table__.c.id)
    )
    result = session.execute(statement)
    inserted_count = len(result.fetchall())
    session.flush()
    return inserted_count


def get_candles(session: Session, symbol: str, timeframe: str) -> list[Candle]:
    """Lee todas las velas guardadas de `symbol`/`timeframe`, ordenadas
    ascendentemente por `open_time`.

    El cálculo de indicadores (`app/data/indicator_calculator.py`) exige
    ese orden: es el mismo orden en que ocurrió el mercado, y ningún
    indicador puede calcularse usando datos que en ese instante todavía no
    existían (sección 15 del plan, look-ahead bias).

    Devuelve `Candle` (el mismo TypedDict de `exchange_client.py`), no el
    modelo ORM `Vela`: así `app/data/indicator_calculator.py`, que consume
    este resultado, no necesita importar SQLAlchemy — igual que
    `candle_ingestion.py` ya mantiene la capa `data` sin depender de la
    capa de persistencia.
    """
    statement = (
        select(Vela)
        .where(Vela.symbol == symbol, Vela.timeframe == timeframe)
        .order_by(Vela.open_time.asc())
    )
    rows = session.execute(statement).scalars().all()
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
        .where(Vela.symbol == symbol, Vela.timeframe == timeframe, Vela.open_time <= open_time_at_or_before)
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
            Vela.open_time >= open_time_from,
            Vela.open_time <= open_time_to,
        )
        .order_by(Vela.open_time.asc())
    ).all()
    return [(r[0], r[1]) for r in rows]


def get_latest_open_time(session: Session, symbol: str, timeframe: str) -> datetime | None:
    """`open_time` de la vela más reciente guardada, o `None` si no hay."""
    return session.execute(
        select(Vela.open_time)
        .where(Vela.symbol == symbol, Vela.timeframe == timeframe)
        .order_by(Vela.open_time.desc())
        .limit(1)
    ).scalar_one_or_none()
