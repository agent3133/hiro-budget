"""category report options: one-off categories and reporting-month shift

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-25 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('category', schema=None) as batch_op:
        batch_op.add_column(sa.Column('one_off', sa.Boolean(), server_default='0', nullable=False))
        batch_op.add_column(
            sa.Column('report_shift_days', sa.Integer(), server_default='0', nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table('category', schema=None) as batch_op:
        batch_op.drop_column('report_shift_days')
        batch_op.drop_column('one_off')
