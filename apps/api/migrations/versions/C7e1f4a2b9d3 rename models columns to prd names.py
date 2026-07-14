"""rename models columns to PRD names

Revision ID: c7e1f4a2b9d3
Revises: b3d8f2a6c9e1
Create Date: 2026-07-14 00:00:00.000000

PRD Section 3.1 requires the following exact physical column names on
`models`, which diverged from the original implementation:

    file_format          -> format
    s3_raw_key           -> raw_s3_key
    s3_processed_prefix  -> processed_s3_prefix

Implemented with plain `ALTER TABLE ... RENAME COLUMN` statements only.
This is a metadata-only operation in PostgreSQL — it does not rewrite the
table, does not touch existing row data, and automatically keeps every
index, constraint, and foreign key that references these columns working
under the new names (Postgres updates the underlying catalog entries for
free; no index/constraint needs to be dropped or recreated).
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c7e1f4a2b9d3'
down_revision = 'b3d8f2a6c9e1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("models", "file_format", new_column_name="format")
    op.alter_column("models", "s3_raw_key", new_column_name="raw_s3_key")
    op.alter_column("models", "s3_processed_prefix", new_column_name="processed_s3_prefix")


def downgrade() -> None:
    op.alter_column("models", "format", new_column_name="file_format")
    op.alter_column("models", "raw_s3_key", new_column_name="s3_raw_key")
    op.alter_column("models", "processed_s3_prefix", new_column_name="s3_processed_prefix")