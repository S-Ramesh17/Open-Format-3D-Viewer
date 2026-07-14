"""remove_redundant_api_keys_key_hash_index

Revision ID: b3d8f2a6c9e1
Revises: a9c3e7f1b2d4
Create Date: 2026-07-14 00:00:00.000000

api_keys.key_hash ended up with two unique indexes after
a9c3e7f1b2d4_add_prd_required_composite_indexes:

  1. api_keys_key_hash_key — auto-created by the column's `unique=True`
     (this was already there before a9c3e7f1b2d4, just previously backing
     a non-unique explicitly-named index too).
  2. ix_api_keys_key_hash  — the explicitly-named index in __table_args__,
     which a9c3e7f1b2d4 made unique=True to close what looked like a
     missing-uniqueness gap. The gap was actually just the explicit
     index's naming/uniqueness being out of sync with the column
     constraint that was already enforcing it.

Two unique indexes on the same single column enforce the identical
constraint twice — pure redundant write/storage overhead, no functional
difference. This drops the explicit one and keeps the column-level one,
which is what SQLAlchemy will always regenerate from the model going
forward (see app/models/api_key.py — __table_args__ no longer declares
ix_api_keys_key_hash).
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'b3d8f2a6c9e1'
down_revision: Union[str, Sequence[str], None] = 'a9c3e7f1b2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # api_keys_key_hash_key (column-level unique=True) stays untouched —
    # this only removes the redundant explicitly-named duplicate.
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")


def downgrade() -> None:
    op.create_index(
        "ix_api_keys_key_hash",
        "api_keys",
        ["key_hash"],
        unique=True,
    )
