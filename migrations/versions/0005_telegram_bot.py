"""telegram bot: links, link codes, cursors, notices

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-05 23:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'telegram_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('telegram_id', sa.BigInteger(), nullable=False),
        sa.Column('linked_at', sa.DateTime(), nullable=False),
        sa.Column('feed_token', sa.String(length=64), nullable=False),
        sa.Column('digest_at', sa.String(length=5), nullable=True),
        sa.Column('digest_sent_on', sa.Date(), nullable=True),
        sa.Column('notify_changes', sa.Boolean(), nullable=False),
        sa.Column('notify_certificates', sa.Boolean(), nullable=False),
        sa.Column('exam_reminder_days', sa.Integer(), nullable=True),
        sa.Column('reminder_minutes', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['student_id'], ['students.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('student_id'),
        sa.UniqueConstraint('telegram_id'),
        sa.UniqueConstraint('feed_token'),
    )
    op.create_table(
        'telegram_link_codes',
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['student_id'], ['students.id']),
        sa.PrimaryKeyConstraint('code_hash'),
    )
    op.create_index('ix_telegram_link_codes_student_id', 'telegram_link_codes', ['student_id'])
    op.create_table(
        'bot_cursors',
        sa.Column('name', sa.String(length=50), nullable=False),
        sa.Column('value', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('name'),
    )
    op.create_table(
        'bot_notices',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=100), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['student_id'], ['students.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('student_id', 'key', name='uq_bot_notices_student_key'),
    )
    op.create_index('ix_bot_notices_student_id', 'bot_notices', ['student_id'])


def downgrade() -> None:
    op.drop_index('ix_bot_notices_student_id', table_name='bot_notices')
    op.drop_table('bot_notices')
    op.drop_table('bot_cursors')
    op.drop_index('ix_telegram_link_codes_student_id', table_name='telegram_link_codes')
    op.drop_table('telegram_link_codes')
    op.drop_table('telegram_links')
