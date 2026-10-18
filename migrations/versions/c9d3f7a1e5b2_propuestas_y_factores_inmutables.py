"""propuestas y factores: la decisión congelada no se modifica (sección 14)

Revision ID: c9d3f7a1e5b2
Revises: b4e8d2a6c1f3
Create Date: 2026-10-17 10:00:00.000000

La regla «una vez creada la propuesta, su decision_snapshot no se modifica» se
cumplía solo por convención (ningún código actualiza esas filas). Ahora la
impone la base de datos con triggers BEFORE UPDATE a nivel de fila:

- `propuestas`: se rechaza el UPDATE si cambia cualquier columna que describe la
  decisión (snapshot, puntajes, tesis, invalidación, símbolo, ciclo, tier...).
- `factores`: se rechaza el UPDATE si cambia el factor, su peso, su puntaje o su
  confianza (los puntajes por factor con los que se calculó el puntaje).

Qué NO se bloquea, a propósito:
- `created_at` y otras columnas que no describen la decisión: se pueden actualizar.
- DELETE: se deja fuera hasta revisar la limpieza de las pruebas y la copia a
  Railway; esta migración solo cierra la modificación retroactiva.
- TRUNCATE: no dispara triggers de fila (reinicio deliberado de una base).

Efecto en `scripts/copy_database.py`: la copia a Railway decide qué tablas son
inmutables mirando si tienen un trigger BEFORE UPDATE; `propuestas` y `factores`
pasan a sincronizarse con ON CONFLICT DO NOTHING (correcto: sus filas no cambian).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = 'c9d3f7a1e5b2'
down_revision: Union[str, None] = 'b4e8d2a6c1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION propuestas_decision_inmutable() RETURNS trigger AS $$
        BEGIN
            IF NEW.ciclo_id IS DISTINCT FROM OLD.ciclo_id
               OR NEW.symbol IS DISTINCT FROM OLD.symbol
               OR NEW.categoria IS DISTINCT FROM OLD.categoria
               OR NEW.horizonte IS DISTINCT FROM OLD.horizonte
               OR NEW.nivel_riesgo IS DISTINCT FROM OLD.nivel_riesgo
               OR NEW.puntaje_base IS DISTINCT FROM OLD.puntaje_base
               OR NEW.calidad_datos IS DISTINCT FROM OLD.calidad_datos
               OR NEW.puntaje_ponderado IS DISTINCT FROM OLD.puntaje_ponderado
               OR NEW.supera_umbral_entrada IS DISTINCT FROM OLD.supera_umbral_entrada
               OR NEW.tier_entrada IS DISTINCT FROM OLD.tier_entrada
               OR NEW.ciclos_consecutivos_en_zona IS DISTINCT FROM OLD.ciclos_consecutivos_en_zona
               OR NEW.segunda_ronda IS DISTINCT FROM OLD.segunda_ronda
               OR NEW.tesis IS DISTINCT FROM OLD.tesis
               OR NEW.invalidacion IS DISTINCT FROM OLD.invalidacion
               OR NEW.escenario_adverso IS DISTINCT FROM OLD.escenario_adverso
               OR NEW.comentario_chartista IS DISTINCT FROM OLD.comentario_chartista
               OR NEW.fuentes IS DISTINCT FROM OLD.fuentes
               OR NEW.confianza_global IS DISTINCT FROM OLD.confianza_global
               OR NEW.decision_snapshot IS DISTINCT FROM OLD.decision_snapshot
            THEN
                RAISE EXCEPTION 'propuestas es de decision congelada: no se modifica el snapshot ni los datos de la decision (solo-agregar)';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_propuestas_decision_inmutable
        BEFORE UPDATE ON propuestas
        FOR EACH ROW EXECUTE FUNCTION propuestas_decision_inmutable()
        """
    )
    op.execute(
        """
        CREATE FUNCTION factores_inmutable() RETURNS trigger AS $$
        BEGIN
            IF NEW.ciclo_id IS DISTINCT FROM OLD.ciclo_id
               OR NEW.propuesta_id IS DISTINCT FROM OLD.propuesta_id
               OR NEW.symbol IS DISTINCT FROM OLD.symbol
               OR NEW.factor IS DISTINCT FROM OLD.factor
               OR NEW.peso IS DISTINCT FROM OLD.peso
               OR NEW.puntaje IS DISTINCT FROM OLD.puntaje
               OR NEW.confianza IS DISTINCT FROM OLD.confianza
            THEN
                RAISE EXCEPTION 'factores es de decision congelada: no se modifica el peso ni el puntaje de un factor (solo-agregar)';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_factores_inmutable
        BEFORE UPDATE ON factores
        FOR EACH ROW EXECUTE FUNCTION factores_inmutable()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_factores_inmutable ON factores")
    op.execute("DROP FUNCTION IF EXISTS factores_inmutable()")
    op.execute("DROP TRIGGER IF EXISTS trg_propuestas_decision_inmutable ON propuestas")
    op.execute("DROP FUNCTION IF EXISTS propuestas_decision_inmutable()")
