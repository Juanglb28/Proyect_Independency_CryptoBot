"""Corrección auditable de trade_results: versionado por encadenamiento

Revision ID: f3a2d9c1b4e7
Revises: e6a1c4b7d902
Create Date: 2026-10-13 00:00:00.000000

Plan §15.3: "los registros históricos de decisión y resultado no se actualizan
para corregir retrospectivamente una decisión; si existe un error de cálculo, se
registra una nueva versión/corrección auditable". Y §37.8 prohíbe modificar
resultados históricos para mejorar performance.

Cómo: la corrección NO toca la fila original (el trigger de solo-agregar sigue
rechazando UPDATE/DELETE). Es una fila NUEVA que apunta a la que corrige
(`supersedes_id`), con un motivo obligatorio (`correction_reason`) y, opcional,
quién la ordenó (`corrected_by`). La versión "vigente" de una posición es la
punta de la cadena (la fila que ninguna otra corrige).

Integridad (decisión del usuario 2026-10-13: trigger en la base):
- Un solo ORIGINAL por posición: índice único parcial sobre position_id donde
  supersedes_id IS NULL (reemplaza al UNIQUE(position_id); conserva la
  idempotencia de save_trade_result).
- Una sola CORRECCIÓN por versión (cadena lineal, sin bifurcaciones): índice
  único parcial sobre supersedes_id. Esto obliga además a que solo se pueda
  corregir la punta: una versión ya corregida ya tiene "hijo".
- Misma posición: un trigger BEFORE INSERT rechaza una corrección cuyo padre
  pertenezca a otra posición (un índice no puede comprobar esto entre filas).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3a2d9c1b4e7"
down_revision: Union[str, None] = "e6a1c4b7d902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trade_results", sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("trade_results", sa.Column("correction_reason", sa.Text(), nullable=True))
    op.add_column("trade_results", sa.Column("corrected_by", sa.Text(), nullable=True))

    op.create_foreign_key(
        "fk_trade_results_supersedes_id_trade_results",
        "trade_results", "trade_results",
        ["supersedes_id"], ["id"],
    )

    # El motivo es obligatorio exactamente cuando la fila ES una corrección.
    op.create_check_constraint(
        "ck_trade_results_motivo_si_corrige",
        "trade_results",
        "(supersedes_id IS NULL) = (correction_reason IS NULL)",
    )

    # La unicidad de position_id deja de ser total: ahora hay varias versiones
    # por posición. Un solo ORIGINAL por posición; una sola corrección por versión.
    op.drop_constraint("uq_trade_results_position_id", "trade_results", type_="unique")
    op.create_index(
        "uq_trade_results_original_por_posicion",
        "trade_results", ["position_id"],
        unique=True, postgresql_where=sa.text("supersedes_id IS NULL"),
    )
    op.create_index(
        "uq_trade_results_una_correccion_por_version",
        "trade_results", ["supersedes_id"],
        unique=True, postgresql_where=sa.text("supersedes_id IS NOT NULL"),
    )

    # Trigger: una corrección debe ser de la MISMA posición que la versión que
    # corrige (lo que un índice no puede comprobar entre filas).
    op.execute(
        """
        CREATE FUNCTION trade_results_cadena_valida() RETURNS trigger AS $$
        DECLARE
            pos_padre UUID;
        BEGIN
            IF NEW.supersedes_id IS NOT NULL THEN
                SELECT position_id INTO pos_padre FROM trade_results WHERE id = NEW.supersedes_id;
                IF pos_padre IS NULL THEN
                    RAISE EXCEPTION 'la versión corregida % no existe', NEW.supersedes_id;
                END IF;
                IF pos_padre <> NEW.position_id THEN
                    RAISE EXCEPTION 'una corrección debe ser de la misma posición (padre %, nueva %)',
                        pos_padre, NEW.position_id;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_trade_results_cadena_valida
        BEFORE INSERT ON trade_results
        FOR EACH ROW EXECUTE FUNCTION trade_results_cadena_valida()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_trade_results_cadena_valida ON trade_results")
    op.execute("DROP FUNCTION trade_results_cadena_valida()")
    op.drop_index("uq_trade_results_una_correccion_por_version", table_name="trade_results")
    op.drop_index("uq_trade_results_original_por_posicion", table_name="trade_results")
    op.create_unique_constraint("uq_trade_results_position_id", "trade_results", ["position_id"])
    op.drop_constraint("ck_trade_results_motivo_si_corrige", "trade_results", type_="check")
    op.drop_constraint("fk_trade_results_supersedes_id_trade_results", "trade_results", type_="foreignkey")
    op.drop_column("trade_results", "corrected_by")
    op.drop_column("trade_results", "correction_reason")
    op.drop_column("trade_results", "supersedes_id")
