"""add enterprise to plan_enum

Revision ID: f1a2b3c4d5e6
Revises: e4f19ac82d67
Create Date: 2026-07-10 08:00:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'f1a2b3c4d5e6'
down_revision = 'e4f19ac82d67'
branch_labels = None
depends_on = None

def upgrade():
    # PostgreSQL requires ALTER TYPE for adding enum values outside of a table alter.
    # Must run outside of standard transaction block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE plan_enum ADD VALUE IF NOT EXISTS 'enterprise'")

def downgrade():
    # PostgreSQL does not support safely dropping ENUM values.
    # Downgrade is a no-op to prevent destructive failures.
    pass