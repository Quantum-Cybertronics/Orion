"""Add params column to agent_runs

Revision ID: b3c7a1f90e44
Revises: 7d2e91c4b8a3
Create Date: 2026-10-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3c7a1f90e44'
down_revision: Union[str, Sequence[str], None] = '7d2e91c4b8a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('agent_runs', sa.Column('params', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('agent_runs') as batch_op:
        batch_op.drop_column('params')
