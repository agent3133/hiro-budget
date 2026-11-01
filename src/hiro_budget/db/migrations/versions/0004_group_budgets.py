"""group budgets: assignments to category groups

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-20 21:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('category_group', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('budget_by_group', sa.Boolean(), server_default='0', nullable=False)
        )

    with op.batch_alter_table('budget_assignment', schema=None) as batch_op:
        batch_op.alter_column('category_id', existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column('group_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f('fk_budget_assignment_group_id_category_group'),
            'category_group', ['group_id'], ['id'],
        )
        batch_op.create_unique_constraint(
            'uq_budget_assignment_month_group', ['month', 'group_id']
        )
        batch_op.create_check_constraint(
            'category_or_group', '(category_id IS NULL) != (group_id IS NULL)'
        )


def downgrade() -> None:
    op.execute('DELETE FROM budget_assignment WHERE group_id IS NOT NULL')
    with op.batch_alter_table('budget_assignment', schema=None) as batch_op:
        batch_op.drop_constraint('category_or_group', type_='check')
        batch_op.drop_constraint('uq_budget_assignment_month_group', type_='unique')
        batch_op.drop_constraint(
            batch_op.f('fk_budget_assignment_group_id_category_group'), type_='foreignkey'
        )
        batch_op.drop_column('group_id')
        batch_op.alter_column('category_id', existing_type=sa.Integer(), nullable=False)

    with op.batch_alter_table('category_group', schema=None) as batch_op:
        batch_op.drop_column('budget_by_group')
