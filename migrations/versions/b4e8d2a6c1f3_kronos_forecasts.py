"""kronos_forecasts: pronósticos del experimento Kronos (enmienda E6, protocolo K1)

Revision ID: b4e8d2a6c1f3
Revises: a7c2e5d1f9b3
Create Date: 2026-10-15 12:00:00.000000

Tabla de EXPERIMENTO en sombra (P2): sin claves foráneas hacia tablas
operativas. Solo-agregar: un trigger rechaza UPDATE y DELETE de filas (los
resultados históricos no se modifican). TRUNCATE no se bloquea: es la forma
deliberada de reiniciar el experimento.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b4e8d2a6c1f3'
down_revision: Union[str, None] = 'a7c2e5d1f9b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'kronos_forecasts',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column('symbol', sa.Text(), nullable=False),
        sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
        sa.Column('model_id', sa.Text(), nullable=False),
        sa.Column('weights_revision', sa.Text(), nullable=False),
        sa.Column('tokenizer_id', sa.Text(), nullable=False),
        sa.Column('tokenizer_revision', sa.Text(), nullable=False),
        sa.Column('code_commit', sa.Text(), nullable=False),
        sa.Column('lookback', sa.Integer(), nullable=False),
        sa.Column('horizon', sa.Integer(), nullable=False),
        sa.Column('n_trajectories', sa.Integer(), nullable=False),
        sa.Column('temperature', sa.Numeric(10, 4), nullable=False),
        sa.Column('top_p', sa.Numeric(10, 4), nullable=False),
        sa.Column('seed', sa.BigInteger(), nullable=False),
        sa.Column('uses_volume', sa.Boolean(), nullable=False),
        sa.Column('device', sa.Text(), nullable=False),
        sa.Column('environment', sa.Text(), nullable=False),
        sa.Column('protocol_sha256', sa.Text(), nullable=False),
        sa.Column('last_close', sa.Numeric(30, 12), nullable=False),
        sa.Column('final_closes', postgresql.ARRAY(sa.Numeric(30, 12)), nullable=False),
        sa.Column('expected_return', sa.Numeric(20, 8), nullable=False),
        sa.Column('prob_up', sa.Numeric(10, 6), nullable=False),
        sa.Column('dispersion', sa.Numeric(20, 8), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('n_trajectories > 0 AND lookback > 0 AND horizon > 0', name='ck_kronos_forecasts_parametros_positivos'),
        sa.CheckConstraint('cardinality(final_closes) = n_trajectories', name='ck_kronos_forecasts_una_cifra_por_trayectoria'),
        sa.CheckConstraint('prob_up >= 0 AND prob_up <= 1', name='ck_kronos_forecasts_prob_up_entre_0_y_1'),
        sa.CheckConstraint('last_close > 0', name='ck_kronos_forecasts_ultimo_cierre_positivo'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'symbol', 'as_of', 'weights_revision', 'lookback', 'horizon', 'n_trajectories', 'seed',
            name='uq_kronos_forecasts_clave_logica',
        ),
    )
    op.create_index('ix_kronos_forecasts_as_of', 'kronos_forecasts', ['as_of'])
    op.execute(
        """
        CREATE FUNCTION kronos_forecasts_solo_agregar() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'kronos_forecasts es de solo-agregar: no se modifica ni se borra un pronostico generado';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_kronos_forecasts_solo_agregar
        BEFORE UPDATE OR DELETE ON kronos_forecasts
        FOR EACH ROW EXECUTE FUNCTION kronos_forecasts_solo_agregar()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_kronos_forecasts_solo_agregar ON kronos_forecasts")
    op.execute("DROP FUNCTION IF EXISTS kronos_forecasts_solo_agregar()")
    op.drop_index('ix_kronos_forecasts_as_of', table_name='kronos_forecasts')
    op.drop_table('kronos_forecasts')
