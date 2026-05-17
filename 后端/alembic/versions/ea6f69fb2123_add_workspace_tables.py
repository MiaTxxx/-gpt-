"""add_workspace_tables

Revision ID: ea6f69fb2123
Revises: 0f8fcd98c935
Create Date: 2026-05-16 15:29:09.635494
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'ea6f69fb2123'
down_revision: Union[str, Sequence[str], None] = '0f8fcd98c935'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(sa.Column('quota_remaining', sa.Integer(), nullable=False, server_default='20'))


def downgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('quota_remaining')
