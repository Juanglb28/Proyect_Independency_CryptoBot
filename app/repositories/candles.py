"""Persistencia de velas (sección 15.7 del plan: idempotencia por
`symbol + timeframe + open_time`).

Esta capa SOLO persiste (sección 15.2: `repositories` no contiene lógica
de negocio). No decide qué símbolo, temporalidad o ventana descargar —
eso vive en `app/data/candle_ingestion.py`.
"""

from __future__ import annotations

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
