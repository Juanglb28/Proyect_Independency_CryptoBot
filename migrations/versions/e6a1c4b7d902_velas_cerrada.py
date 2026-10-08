"""velas.cerrada: velas en curso vs. cerradas, y velas cerradas inmutables

Revision ID: e6a1c4b7d902
Revises: b7d41c9e2a60
Create Date: 2026-10-08 12:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6a1c4b7d902'
down_revision: Union[str, None] = 'b7d41c9e2a60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Aprobado por el usuario (2026-10-08, "A y B"): la ingesta guardaba la vela
    # EN CURSO con ON CONFLICT DO NOTHING y la congelaba para siempre con los
    # valores del instante de la descarga (una vela por temporalidad por dia).
    # Columna nueva: todas las filas existentes arrancan como cerradas...
    op.add_column(
        'velas',
        sa.Column('cerrada', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    )
    # ...salvo las que se guardaron ANTES de que la vela cerrara: created_at
    # (instante real del INSERT) < open_time + duracion. Esas tienen valores
    # parciales y quedan marcadas como NO cerradas; el script
    # `scripts/repair_partial_candles.py` las vuelve a descargar y corrige.
    op.execute(
        """
        UPDATE velas SET cerrada = false
        WHERE created_at < open_time + CASE timeframe
            WHEN '1h' THEN interval '1 hour'
            WHEN '4h' THEN interval '4 hours'
            WHEN '12h' THEN interval '12 hours'
            WHEN '1d' THEN interval '1 day'
        END
        """
    )
    # Una vela cerrada no se modifica nunca (mismo criterio que las tablas
    # historicas del sistema): el trigger rechaza cualquier UPDATE sobre una
    # fila con cerrada = true. DELETE no se restringe aqui.
    op.execute(
        """
        CREATE FUNCTION velas_cerrada_inmutable() RETURNS trigger AS $$
        BEGIN
            IF OLD.cerrada THEN
                RAISE EXCEPTION 'velas: una vela cerrada es inmutable (% % %)',
                    OLD.symbol, OLD.timeframe, OLD.open_time;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER velas_cerrada_inmutable
        BEFORE UPDATE ON velas
        FOR EACH ROW EXECUTE FUNCTION velas_cerrada_inmutable()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS velas_cerrada_inmutable ON velas")
    op.execute("DROP FUNCTION IF EXISTS velas_cerrada_inmutable()")
    op.drop_column('velas', 'cerrada')
