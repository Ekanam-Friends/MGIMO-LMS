"""announcements: stream (program + course) and single-student audiences

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-10 20:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('announcements') as b:
        b.add_column(sa.Column('program', sa.String(length=500), nullable=True))
        b.add_column(sa.Column('course', sa.Integer(), nullable=True))
        b.add_column(sa.Column('student_id', sa.Integer(), nullable=True))
        b.create_foreign_key('fk_announcements_student_id', 'students', ['student_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('announcements') as b:
        b.drop_constraint('fk_announcements_student_id', type_='foreignkey')
        b.drop_column('student_id')
        b.drop_column('course')
        b.drop_column('program')
