"""notifications: announcements, notification_reads

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-10 18:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'announcements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('author_user_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('audience', sa.String(length=20), nullable=False),
        sa.Column('faculty_name', sa.String(length=500), nullable=True),
        sa.Column('group_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['author_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['group_id'], ['study_groups.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_announcements_author_user_id', 'announcements', ['author_user_id'])
    op.create_index('ix_announcements_created_at', 'announcements', ['created_at'])
    op.create_table(
        'notification_reads',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=40), nullable=False),
        sa.Column('read_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('user_id', 'key'),
    )


def downgrade() -> None:
    op.drop_table('notification_reads')
    op.drop_table('announcements')
