"""Persistencia de `kronos_forecasts` (experimento Kronos, protocolo K1).

Solo persiste (sección 15.2): no decide qué fechas pronosticar, no resume
trayectorias ni evalúa nada (eso es de las reglas de `kronos_signal` y de los
scripts). Idempotente: `append_forecast` usa `INSERT ... ON CONFLICT DO NOTHING`
sobre la clave lógica, así que reanudar o repetir la generación no duplica (P5).

La transacción la maneja quien llama.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models.kronos_forecasts import KronosForecast


@dataclass(frozen=True)
class ForecastRow:
    """Un pronóstico listo para persistir (o leído). Sin lógica de negocio."""

    symbol: str
    as_of: datetime
    model_id: str
    weights_revision: str
    tokenizer_id: str
    tokenizer_revision: str
    code_commit: str
    lookback: int
    horizon: int
    n_trajectories: int
    temperature: Decimal
    top_p: Decimal
    seed: int
    uses_volume: bool
    device: str
    environment: str
    protocol_sha256: str
    last_close: Decimal
    final_closes: tuple[Decimal, ...]
    expected_return: Decimal
    prob_up: Decimal
    dispersion: Decimal | None


def append_forecast(session: Session, row: ForecastRow) -> bool:
    """Agrega el pronóstico si su clave lógica es nueva. `True` si lo escribió,
    `False` si ya existía (no modifica nada)."""
    resultado = session.execute(
        pg_insert(KronosForecast)
        .values(
            symbol=row.symbol,
            as_of=row.as_of,
            model_id=row.model_id,
            weights_revision=row.weights_revision,
            tokenizer_id=row.tokenizer_id,
            tokenizer_revision=row.tokenizer_revision,
            code_commit=row.code_commit,
            lookback=row.lookback,
            horizon=row.horizon,
            n_trajectories=row.n_trajectories,
            temperature=row.temperature,
            top_p=row.top_p,
            seed=row.seed,
            uses_volume=row.uses_volume,
            device=row.device,
            environment=row.environment,
            protocol_sha256=row.protocol_sha256,
            last_close=row.last_close,
            final_closes=list(row.final_closes),
            expected_return=row.expected_return,
            prob_up=row.prob_up,
            dispersion=row.dispersion,
        )
        .on_conflict_do_nothing(constraint="uq_kronos_forecasts_clave_logica")
        .returning(KronosForecast.id)
    ).first()
    return resultado is not None


def claves_existentes(
    session: Session, *, weights_revision: str, lookback: int, horizon: int, n_trajectories: int, seed: int
) -> set[tuple[str, datetime]]:
    """`{(símbolo, as_of)}` ya generados con esos parámetros: lo que la reanudación se salta."""
    filas = session.execute(
        select(KronosForecast.symbol, KronosForecast.as_of).where(
            KronosForecast.weights_revision == weights_revision,
            KronosForecast.lookback == lookback,
            KronosForecast.horizon == horizon,
            KronosForecast.n_trajectories == n_trajectories,
            KronosForecast.seed == seed,
        )
    ).all()
    return {(s, a) for s, a in filas}


def listar_forecasts(
    session: Session, *, weights_revision: str, lookback: int, horizon: int, n_trajectories: int, seed: int
) -> Sequence[ForecastRow]:
    """Todos los pronósticos de esa configuración, ordenados por (as_of, símbolo)."""
    filas = session.execute(
        select(KronosForecast)
        .where(
            KronosForecast.weights_revision == weights_revision,
            KronosForecast.lookback == lookback,
            KronosForecast.horizon == horizon,
            KronosForecast.n_trajectories == n_trajectories,
            KronosForecast.seed == seed,
        )
        .order_by(KronosForecast.as_of, KronosForecast.symbol)
    ).scalars().all()
    return [
        ForecastRow(
            symbol=f.symbol, as_of=f.as_of, model_id=f.model_id, weights_revision=f.weights_revision,
            tokenizer_id=f.tokenizer_id, tokenizer_revision=f.tokenizer_revision, code_commit=f.code_commit,
            lookback=f.lookback, horizon=f.horizon, n_trajectories=f.n_trajectories, temperature=f.temperature,
            top_p=f.top_p, seed=f.seed, uses_volume=f.uses_volume, device=f.device, environment=f.environment,
            protocol_sha256=f.protocol_sha256, last_close=f.last_close, final_closes=tuple(f.final_closes),
            expected_return=f.expected_return, prob_up=f.prob_up, dispersion=f.dispersion,
        )
        for f in filas
    ]
