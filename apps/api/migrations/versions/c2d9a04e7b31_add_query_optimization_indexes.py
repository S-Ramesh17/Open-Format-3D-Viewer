"""add_query_optimization_indexes

Revision ID: c2d9a04e7b31
Revises: b7e4f91a3c58
Create Date: 2026-07-02 09:00:00.000000

Week 3 Day 4 performance indexes:
  - annotations (model_id, status)  — filters list queries by status
  - model_elements (element_type)   — filters by IFC type
  - models (uploaded_by)            — user-scoped model queries
  - annotation_comments (annotation_id) — already in initial schema, verify here
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'c2d9a04e7b31'
down_revision: Union[str, Sequence[str], None] = 'b7e4f91a3c58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Composite index for list_annotations(model_id, status=?) — the hottest query path
    op.create_index(
        'ix_annotations_model_status',
        'annotations',
        ['model_id', 'status'],
    )
    # Index for element_type filter in list_elements
    op.create_index(
        'ix_model_elements_element_type',
        'model_elements',
        ['element_type'],
    )
    # Index for user-scoped model queries (uploaded_by)
    op.create_index(
        'ix_models_uploaded_by',
        'models',
        ['uploaded_by'],
    )


def downgrade() -> None:
    op.drop_index('ix_models_uploaded_by', table_name='models')
    op.drop_index('ix_model_elements_element_type', table_name='model_elements')
    op.drop_index('ix_annotations_model_status', table_name='annotations')
