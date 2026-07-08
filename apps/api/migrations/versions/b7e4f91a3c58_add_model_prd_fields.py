"""add_model_prd_fields

Revision ID: b7e4f91a3c58
Revises: a3c1e8f02b47
Create Date: 2026-06-30 09:00:00.000000

Adds PRD-required fields to the models table:
  - name              (display name, defaults to original_filename)
  - element_count     (populated by ifc.py on ready)
  - bounds_min_xyz    (JSONB array [x,y,z], populated by ifc.py on ready)
  - bounds_max_xyz    (JSONB array [x,y,z], populated by ifc.py on ready)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b7e4f91a3c58'
down_revision: Union[str, Sequence[str], None] = 'a3c1e8f02b47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('models', sa.Column('name', sa.String(length=500), nullable=True))
    op.add_column('models', sa.Column('element_count', sa.BigInteger(), nullable=True))
    op.add_column(
        'models',
        sa.Column('bounds_min_xyz', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        'models',
        sa.Column('bounds_max_xyz', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # Backfill name from original_filename for existing rows
    op.execute("UPDATE models SET name = original_filename WHERE name IS NULL")


def downgrade() -> None:
    op.drop_column('models', 'bounds_max_xyz')
    op.drop_column('models', 'bounds_min_xyz')
    op.drop_column('models', 'element_count')
    op.drop_column('models', 'name')