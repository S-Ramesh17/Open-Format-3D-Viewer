"""add_webhook_delivery_logs

Revision ID: a3c1e8f02b47
Revises: 9f5dabb64afa
Create Date: 2026-06-29 08:00:00.000000

Creates the webhook_delivery_logs table referenced by
apps/worker/app/tasks/webhook.py:_log_delivery().

Without this table every webhook delivery attempt silently swallows
the INSERT exception (the task has a bare try/except that logs at DEBUG
level) — delivery audit logs are lost.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3c1e8f02b47'
down_revision: Union[str, Sequence[str], None] = '9f5dabb64afa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'webhook_delivery_logs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column(
            'webhook_id',
            sa.UUID(),
            sa.ForeignKey('webhooks.id', ondelete='CASCADE'),
            nullable=False,
        ),
        # delivery_id is a UUID generated per attempt (not a FK — it lives
        # only in the log for correlation with external systems)
        sa.Column('delivery_id', sa.String(length=36), nullable=False),
        sa.Column('event', sa.String(length=255), nullable=False),
        sa.Column('status_code', sa.Integer(), nullable=True),
        sa.Column('success', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    # Index for fast per-webhook audit queries
    op.create_index(
        'ix_webhook_delivery_logs_webhook_id',
        'webhook_delivery_logs',
        ['webhook_id'],
    )
    # Index for correlation by delivery_id
    op.create_index(
        'ix_webhook_delivery_logs_delivery_id',
        'webhook_delivery_logs',
        ['delivery_id'],
    )
    # Index for time-based queries (latest deliveries for a webhook)
    op.create_index(
        'ix_webhook_delivery_logs_created_at',
        'webhook_delivery_logs',
        ['created_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_webhook_delivery_logs_created_at', table_name='webhook_delivery_logs')
    op.drop_index('ix_webhook_delivery_logs_delivery_id', table_name='webhook_delivery_logs')
    op.drop_index('ix_webhook_delivery_logs_webhook_id', table_name='webhook_delivery_logs')
    op.drop_table('webhook_delivery_logs')
