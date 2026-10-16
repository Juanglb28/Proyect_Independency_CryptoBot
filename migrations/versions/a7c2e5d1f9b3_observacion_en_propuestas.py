"""Operaciones de observación: `propuestas.calidad_datos` puede faltar solo en ellas

Revision ID: a7c2e5d1f9b3
Revises: f3a2d9c1b4e7
Create Date: 2026-10-13 12:00:00.000000

Decisión del usuario (2026-10-12): cada candidato del pre-filtro que no abre una
entrada de la estrategia abre una posición paper de OBSERVACIÓN, sin IA. Su fila
en `propuestas` (tier `OBSERVACION`) no pasa por el Auditor, así que no tiene una
calidad de datos: se deja NULL en vez de inventar un número (P3).

Cambios:
- `propuestas.calidad_datos` pasa a admitir NULL.
- CHECK `calidad_datos_salvo_observacion`: NULL solo si el tier es `OBSERVACION`.
  Toda propuesta de la estrategia sigue exigiendo su calidad de datos.

`tier_entrada` es VARCHAR(16) validado en Python (no un ENUM nativo): el valor
`OBSERVACION` (11 caracteres) cabe y no hace falta `ALTER TYPE`. No toca datos
existentes: ninguna fila actual tiene `calidad_datos` NULL, así que el CHECK se
cumple desde el primer momento.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7c2e5d1f9b3"
down_revision: Union[str, None] = "f3a2d9c1b4e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("propuestas", "calidad_datos", existing_type=sa.Numeric(5, 2), nullable=True)
    op.create_check_constraint(
        "calidad_datos_salvo_observacion",
        "propuestas",
        "calidad_datos IS NOT NULL OR tier_entrada = 'OBSERVACION'",
    )


def downgrade() -> None:
    # Solo es posible si no existen observaciones (su calidad de datos es NULL).
    op.drop_constraint("calidad_datos_salvo_observacion", "propuestas", type_="check")
    op.alter_column("propuestas", "calidad_datos", existing_type=sa.Numeric(5, 2), nullable=False)
