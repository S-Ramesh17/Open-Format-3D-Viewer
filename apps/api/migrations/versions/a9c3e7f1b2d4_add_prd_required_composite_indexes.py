"""add_prd_required_composite_indexes

Revision ID: a9c3e7f1b2d4
Revises: f1a2b3c4d5e6
Create Date: 2026-07-14 00:00:00.000000

P1 remediation — verified against existing migrations (10edba2f92ae initial
schema, c2d9a04e7b31 query-optimization indexes) rather than assumed. Actual
gaps found:

  - models: only single-column (project_id) and (status) indexes existed —
    no composite (project_id, status) or (project_id, created_at DESC).
  - model_elements: (model_id, guid) composite already existed; (model_id,
    element_type) did not — only a single-column (element_type) index did.
  - annotations: (model_id, status) composite already existed
    (ix_annotations_model_status); (model_id, created_at DESC) did not.
  - api_keys.key_hash: an index existed (ix_api_keys_key_hash) but it was
    NOT unique — a hash collision could silently insert two active keys
    sharing one key_hash. Replaced with a unique index rather than adding a
    redundant second one.

This migration only adds/replaces indexes — no table or column changes,
no data migration, fully additive and reversible.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'a9c3e7f1b2d4'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # models: hot paths are "list models in project X with status Y" and
    # "list models in project X ordered by newest first".
    op.create_index(
        'ix_models_project_status',
        'models',
        ['project_id', 'status'],
    )
    op.create_index(
        'ix_models_project_created_at',
        'models',
        ['project_id', 'created_at'],
        postgresql_ops={'created_at': 'DESC'},
    )

    # model_elements: element-type filter is always scoped to one model
    # (list_elements(model_id, ifc_type=?)) — the existing single-column
    # index on element_type alone forces a bitmap-and with model_id on
    # every query instead of a single composite index scan.
    op.create_index(
        'ix_model_elements_model_element_type',
        'model_elements',
        ['model_id', 'element_type'],
    )

    # annotations: "list annotations for model X, newest first" — the
    # existing (model_id, status) composite doesn't help when no status
    # filter is applied.
    op.create_index(
        'ix_annotations_model_created_at',
        'annotations',
        ['model_id', 'created_at'],
        postgresql_ops={'created_at': 'DESC'},
    )

    # api_keys.key_hash must be unique — a non-unique index on a hash
    # column that's used for auth lookup is a correctness gap, not just a
    # performance one. Drop the old non-unique index and replace it with a
    # unique one under the same conceptual purpose. Assumes no existing
    # duplicate key_hash rows (key_hash values are generated from a
    # cryptographically random secret, so a real collision is not
    # expected in practice); if this upgrade fails with a uniqueness
    # violation in a live environment, that indicates an actual data
    # problem to investigate before retrying, not a bug in this migration.
    op.drop_index('ix_api_keys_key_hash', table_name='api_keys')
    op.create_index(
        'ix_api_keys_key_hash',
        'api_keys',
        ['key_hash'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index('ix_api_keys_key_hash', table_name='api_keys')
    op.create_index(
        'ix_api_keys_key_hash',
        'api_keys',
        ['key_hash'],
        unique=False,
    )
    op.drop_index('ix_annotations_model_created_at', table_name='annotations')
    op.drop_index('ix_model_elements_model_element_type', table_name='model_elements')
    op.drop_index('ix_models_project_created_at', table_name='models')
    op.drop_index('ix_models_project_status', table_name='models')
