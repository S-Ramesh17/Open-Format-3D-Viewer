"""rename users.full_name to name

Revision ID: d8e3a15c7f92
Revises: c7e1f4a2b9d3
Create Date: 2026-07-14 00:00:00.000000

PRD Section 2 ("Database schema") specifies the users table as:
    id (uuid PK), email (unique), name, plan, created_at, updated_at

The original implementation used `full_name` instead of `name`. This
follows the exact pattern already used by c7e1f4a2b9d3 (models column
rename): a plain `ALTER TABLE ... RENAME COLUMN`, which is metadata-only
in PostgreSQL — no table rewrite, no data touched, and every index/
constraint/FK referencing this column keeps working automatically.
full_name has no index or FK referencing it (verified against the
initial_schema migration before writing this), so this is a pure
rename with no additional catalog changes required.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd8e3a15c7f92'
down_revision = 'c7e1f4a2b9d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("users", "full_name", new_column_name="name")


def downgrade() -> None:
    op.alter_column("users", "name", new_column_name="full_name")