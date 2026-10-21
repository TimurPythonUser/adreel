"""seeds for video model

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-16 15:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default: в таблицах уже есть ролики и клипы, NOT NULL без значения упадёт
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(sa.Column('seed', sa.Integer(), nullable=False,
                                      server_default='0'))
    with op.batch_alter_table('clips', schema=None) as batch_op:
        batch_op.add_column(sa.Column('seed', sa.Integer(), nullable=False,
                                      server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('clips', schema=None) as batch_op:
        batch_op.drop_column('seed')
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('seed')
