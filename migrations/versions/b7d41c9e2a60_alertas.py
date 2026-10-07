"""alertas

Revision ID: b7d41c9e2a60
Revises: a3c2e5195411
Create Date: 2026-10-07 18:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d41c9e2a60'
down_revision: Union[str, None] = 'a3c2e5195411'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Tabla NUEVA y vacia (aprobada por el usuario, 2026-10-07): avisos para el
    # usuario por Telegram (Paso 11a). Ajuste manual sobre lo autogenerado: un
    # trigger que rechaza DELETE y cualquier cambio del CONTENIDO del aviso;
    # solo el recibo de entrega (delivered_at, delivery_attempts,
    # last_delivery_error) puede actualizarse, y solo hacia adelante.
    op.create_table('alertas',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('tipo', sa.Enum('CAIDA_1H', 'VOLUMEN_ATIPICO', 'ORDEN_PENDIENTE_APROBACION', 'BINANCE_CAIDO', 'BINANCE_RESTABLECIDO', 'DISCREPANCIA_RECONCILIACION', 'PAUSA_DRAWDOWN', name='tipo_alerta', native_enum=False, length=32), nullable=False),
    sa.Column('subject', sa.Text(), nullable=False),
    sa.Column('dedup_key', sa.Text(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('observed_value', sa.Numeric(precision=30, scale=12), nullable=True),
    sa.Column('threshold', sa.Numeric(precision=30, scale=12), nullable=True),
    sa.Column('position_id', sa.UUID(), nullable=True),
    sa.Column('order_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delivery_attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_delivery_error', sa.Text(), nullable=True),
    sa.CheckConstraint('delivered_at IS NULL OR delivery_attempts >= 1', name=op.f('ck_alertas_entregada_implica_intento')),
    sa.CheckConstraint('delivery_attempts >= 0', name=op.f('ck_alertas_intentos_no_negativos')),
    sa.ForeignKeyConstraint(['order_id'], ['ordenes.id'], name=op.f('fk_alertas_order_id_ordenes')),
    sa.ForeignKeyConstraint(['position_id'], ['posiciones.id'], name=op.f('fk_alertas_position_id_posiciones')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_alertas')),
    sa.UniqueConstraint('dedup_key', name=op.f('uq_alertas_dedup_key'))
    )
    op.create_index('ix_alertas_pendientes_de_entrega', 'alertas', ['id'], unique=False, postgresql_where=sa.text('delivered_at IS NULL'))
    op.create_index('ix_alertas_tipo_id', 'alertas', ['tipo', 'id'], unique=False)

    op.execute(
        """
        CREATE FUNCTION alertas_inmutables() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'alertas es de solo-agregar: no se permite DELETE de filas';
            END IF;
            IF (NEW.id, NEW.tipo, NEW.subject, NEW.dedup_key, NEW.occurred_at, NEW.message,
                NEW.observed_value, NEW.threshold, NEW.position_id, NEW.order_id, NEW.created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.tipo, OLD.subject, OLD.dedup_key, OLD.occurred_at, OLD.message,
                OLD.observed_value, OLD.threshold, OLD.position_id, OLD.order_id, OLD.created_at) THEN
                RAISE EXCEPTION 'alertas: el contenido de un aviso no se modifica; solo su recibo de entrega';
            END IF;
            IF OLD.delivered_at IS NOT NULL AND NEW.delivered_at IS DISTINCT FROM OLD.delivered_at THEN
                RAISE EXCEPTION 'alertas: un aviso entregado no vuelve a pendiente ni cambia su fecha de entrega';
            END IF;
            IF NEW.delivery_attempts < OLD.delivery_attempts THEN
                RAISE EXCEPTION 'alertas: los intentos de entrega no pueden disminuir';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_alertas_inmutables
        BEFORE UPDATE OR DELETE ON alertas
        FOR EACH ROW EXECUTE FUNCTION alertas_inmutables()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_alertas_inmutables ON alertas")
    op.execute("DROP FUNCTION alertas_inmutables()")
    op.drop_index('ix_alertas_tipo_id', table_name='alertas')
    op.drop_index('ix_alertas_pendientes_de_entrega', table_name='alertas')
    op.drop_table('alertas')
