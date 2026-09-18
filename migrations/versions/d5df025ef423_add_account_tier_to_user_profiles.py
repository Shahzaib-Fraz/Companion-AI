"""Add account_tier to user_profiles

Revision ID: d5df025ef423
Revises: 001
Create Date: 2026-09-17 13:40:26.755467

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5df025ef423'
down_revision: Union[str, Sequence[str], None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_profiles', 
        sa.Column('account_tier', sa.String(20), nullable=False, server_default='free')
    )

def downgrade() -> None:
    op.drop_column('user_profiles', 'account_tier')