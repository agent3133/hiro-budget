"""change journal

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-05 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('change_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.String(length=32), nullable=False),
    sa.Column('source', sa.String(length=10), nullable=False),
    sa.Column('operation', sa.String(length=60), nullable=False),
    sa.Column('args_json', sa.Text(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('undoable', sa.Boolean(), nullable=False),
    sa.Column('undone_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_change_log'))
    )
    with op.batch_alter_table('change_log', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_change_log_session_id'), ['session_id'], unique=False)

    op.create_table('change_item',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('log_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=4), nullable=False),
    sa.Column('table_name', sa.String(length=40), nullable=True),
    sa.Column('row_id', sa.Integer(), nullable=True),
    sa.Column('path', sa.String(length=200), nullable=True),
    sa.Column('before_json', sa.Text(), nullable=True),
    sa.Column('after_json', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['log_id'], ['change_log.id'], name=op.f('fk_change_item_log_id_change_log'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_change_item'))
    )
    with op.batch_alter_table('change_item', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_change_item_log_id'), ['log_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('change_item', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_change_item_log_id'))

    op.drop_table('change_item')
    with op.batch_alter_table('change_log', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_change_log_session_id'))

    op.drop_table('change_log')
