"""Persistencia de indicadores (sección 15.7 del plan: misma idempotencia
que `velas`, aquí por `symbol + timeframe + calculated_at + indicator_name`).

Esta capa SOLO persiste (sección 15.2: `repositories` no contiene lógica
de negocio). No calcula nada — eso vive en
`app/data/indicator_calculator.py`.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.data.indicator_calculator import IndicatorRow
from app.db.models.indicators import Indicador

# Postgres tiene un límite duro de 65.535 parámetros bind por consulta (es
# un protocolo de 16 bits). Cada fila de `indicadores` usa 5 columnas, así
# que el máximo teórico de filas en un solo INSERT es 65535 // 5 = 13.107.
#
# En la práctica, una sola combinación símbolo/temporalidad puede superar
# eso ampliamente: `calculate_indicators_for_symbol_timeframe` calcula TODOS
# los valores de indicadores de todo el historial de velas de una vez (para
# `1h` sobre ~2 años son ~17.500 velas x ~6 indicadores ≈ 100.000+ filas) y
# se los pasa a `save_indicators` en una sola lista. Sin batching, eso arma
# un único INSERT con 500.000+ parámetros y Postgres lo rechaza.
#
# 2.000 es un margen holgado por debajo del máximo teórico (deja espacio si
# en el futuro se agrega alguna columna más sin tener que recalcular esta
# constante).
_MAX_ROWS_PER_INSERT = 2_000


def save_indicators(session: Session, rows: list[IndicatorRow]) -> int:
    """Inserta `rows`, ignorando las que ya existan.

    Mismo patrón que `repositories/candles.py::save_candles`: `ON CONFLICT
    DO NOTHING` sobre la restricción única, con `.returning()` explícito en
    vez de confiar en `result.rowcount` (que queda en -1 con psycopg3
    cuando SQLAlchemy agrega un RETURNING implícito para leer el `id`
    autogenerado — ver el docstring de `save_candles` para el detalle
    completo de por qué).

    A diferencia de `save_candles`, aquí SÍ hace falta trocear `rows` en
    lotes de `_MAX_ROWS_PER_INSERT`: `save_candles` nunca recibe más de un
    puñado de miles de velas por llamada porque `candle_ingestion.py` ya
    pagina las descargas de Binance, pero `indicator_pipeline.py` calcula y
    guarda el historial completo de una combinación símbolo/temporalidad de
    una sola vez (ver comentario de `_MAX_ROWS_PER_INSERT` arriba). Cada
    lote es su propio `INSERT ... ON CONFLICT DO NOTHING`; ninguno depende
    de los otros, así que trocear no cambia el resultado ni la idempotencia,
    solo evita superar el límite de parámetros de Postgres.
    """
    if not rows:
        return 0

    inserted_count = 0
    for start in range(0, len(rows), _MAX_ROWS_PER_INSERT):
        batch = rows[start : start + _MAX_ROWS_PER_INSERT]
        statement = (
            pg_insert(Indicador.__table__)
            .values(
                [
                    {
                        "symbol": row.symbol,
                        "timeframe": row.timeframe,
                        "calculated_at": row.calculated_at,
                        "indicator_name": row.indicator_name,
                        "value": row.value,
                    }
                    for row in batch
                ]
            )
            .on_conflict_do_nothing(
                constraint="uq_indicadores_symbol_timeframe_calculated_at_indicator_name"
            )
            .returning(Indicador.__table__.c.id)
        )
        result = session.execute(statement)
        inserted_count += len(result.fetchall())

    session.flush()
    return inserted_count
