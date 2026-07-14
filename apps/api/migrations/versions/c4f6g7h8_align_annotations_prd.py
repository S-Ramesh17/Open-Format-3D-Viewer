"""align annotations prd

Revision ID: c4f6g7h8
Revises: d8e3a15c7f92
Create Date: 2026-07-14 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'c4f6g7h8'
down_revision = 'd8e3a15c7f92'
branch_labels = None
depends_on = None

def upgrade():
    # 1. Rename columns safely
    op.alter_column('annotations', 'created_by', new_column_name='author_id')
    op.alter_column('annotations', 'body', new_column_name='message')
    op.alter_column('annotations', 'position', new_column_name='position_xyz')
    
    # 2. Add new columns (bcf_guid added as nullable first for data preservation)
    op.add_column('annotations', sa.Column('normal_xyz', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('annotations', sa.Column('bcf_guid', sa.String(length=36), nullable=True))
    
    # 3. Backfill bcf_guid for any existing rows to satisfy constraints
    op.execute("UPDATE annotations SET bcf_guid = gen_random_uuid()::text WHERE bcf_guid IS NULL")
    
    # 4. Enforce constraints on bcf_guid
    op.alter_column('annotations', 'bcf_guid', nullable=False)
    op.create_unique_constraint('uq_annotations_bcf_guid', 'annotations', ['bcf_guid'])
    
    # 5. Drop the old title column
    op.drop_column('annotations', 'title')
    
    # 6. Update Indexes
    op.drop_index('ix_annotations_created_by', table_name='annotations')
    op.create_index('ix_annotations_author_id', 'annotations', ['author_id'], unique=False)

def downgrade():
    op.drop_index('ix_annotations_author_id', table_name='annotations')
    op.create_index('ix_annotations_created_by', 'annotations', ['author_id'], unique=False)
    
    op.add_column('annotations', sa.Column('title', sa.VARCHAR(length=500), nullable=True))
    op.execute("UPDATE annotations SET title = COALESCE(message, 'Untitled Annotation') WHERE title IS NULL")
    op.alter_column('annotations', 'title', nullable=False)
    
    op.drop_constraint('uq_annotations_bcf_guid', 'annotations', type_='unique')
    op.drop_column('annotations', 'bcf_guid')
    op.drop_column('annotations', 'normal_xyz')
    
    op.alter_column('annotations', 'position_xyz', new_column_name='position')
    op.alter_column('annotations', 'message', new_column_name='body')
    op.alter_column('annotations', 'author_id', new_column_name='created_by')