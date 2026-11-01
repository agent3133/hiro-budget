"""valuations: securities account statements with holdings

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('import_batch', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('valuation', sa.Boolean(), server_default='0', nullable=False)
        )

    op.create_table(
        'holding',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('batch_id', sa.Integer(), nullable=False),
        sa.Column('sort', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('isin', sa.String(length=12), nullable=True),
        sa.Column('wkn', sa.String(length=6), nullable=True),
        sa.Column('quantity', sa.Float(), nullable=False),
        sa.Column('unit', sa.String(length=20), nullable=False),
        sa.Column('price', sa.Float(), nullable=True),
        sa.Column('price_currency', sa.String(length=3), nullable=True),
        sa.Column('value_cents', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ['batch_id'], ['import_batch.id'],
            name=op.f('fk_holding_batch_id_import_batch'), ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_holding')),
    )
    with op.batch_alter_table('holding', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_holding_batch_id'), ['batch_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('holding', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_holding_batch_id'))
    op.drop_table('holding')
    op.execute('DELETE FROM import_batch WHERE valuation = 1')
    with op.batch_alter_table('import_batch', schema=None) as batch_op:
        batch_op.drop_column('valuation')
