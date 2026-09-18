"""Add WhatsApp phone verification fields to user_profiles

P1-17 FIX: supports the new two-phase WhatsApp onboarding flow (send a
code, require it back before saving phone_number) instead of trusting
whatever number was typed in.

Revision ID: 002
Revises: d5df025ef423
Create Date: 2026-09-17

NOTE: originally generated with down_revision='001'. That created two
heads, because this repo has a migration after 001 that wasn't shared
when this file was written - d5df025ef423, "Add account_tier to
user_profiles". Repointed below so the chain is linear:
001 -> d5df025ef423 -> 002.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '002'
down_revision: Union[str, Sequence[str], None] = 'd5df025ef423'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_profiles', sa.Column('pending_phone', sa.String(20), nullable=True))
    op.add_column('user_profiles', sa.Column('pending_phone_code', sa.String(10), nullable=True))
    op.add_column('user_profiles', sa.Column('pending_phone_code_expires_at', sa.DateTime(), nullable=True))
    op.add_column(
        'user_profiles',
        sa.Column('pending_phone_attempts', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('user_profiles', 'pending_phone_attempts')
    op.drop_column('user_profiles', 'pending_phone_code_expires_at')
    op.drop_column('user_profiles', 'pending_phone_code')
    op.drop_column('user_profiles', 'pending_phone')