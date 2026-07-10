"""add in_review to annotations

Revision ID: e4f19ac82d67
Revises: d4f0b12e9a67
Create Date: 2026-07-09 23:17:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'e4f19ac82d67'
down_revision = 'd4f0b12e9a67'
branch_labels = None
depends_on = None

def upgrade():
    # PostgreSQL requires ALTER TYPE for adding enum values outside of a table alter.
    # Must run outside of standard transaction block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE annotation_status_enum ADD VALUE IF NOT EXISTS 'in_review'")

def downgrade():
    # PostgreSQL does not support safely dropping ENUM values. 
    # Downgrade is a no-op to prevent destructive failures.
    pass